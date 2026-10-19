"""Shared, isolated mGBA execution for regression checks and crash probes."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


KEY_RE = re.compile(r'(A|B|START|SELECT|UP|DOWN|LEFT|RIGHT|L|R)@'
                    r'(\d+)(?:-(\d+))?(?::(\d+))?(?::(\d+))?')
RAM_SIZES = {'ewram': 0x40000, 'iwram': 0x8000, 'vram': 0x18000}


def expand_keys(tokens, frames=None):
    """Validate key scripts before the harness can silently ignore/truncate them."""
    if isinstance(tokens, str):
        tokens = tokens.split()
    if not isinstance(tokens, list):
        raise ValueError('keys 必须是按键列表或空格分隔的字符串')
    out = []
    for token in tokens:
        m = KEY_RE.fullmatch(token) if isinstance(token, str) else None
        if not m:
            raise ValueError(f'无效按键：{token!r}')
        key, lo, hi, hold, period = m.groups()
        lo, hi = int(lo), int(hi) if hi is not None else int(lo)
        hold, step = int(hold or 4), int(period or 40)
        if hold <= 0 or step <= 0 or hi < lo:
            raise ValueError(f'按键时长/周期必须为正，区间不能倒序：{token}')
        if period is not None and '-' not in token:
            raise ValueError(f'单次按键不能指定周期：{token}')
        count = (hi - lo) // step + 1
        if len(out) + count > 4096:
            raise ValueError('按键展开后超过 harness 的 4096 事件上限')
        if frames is not None and lo + (count - 1) * step >= frames:
            raise ValueError(f'按键超出运行帧数 {frames}：{token}')
        out.extend(f'{key}@{f}:{hold}' for f in range(lo, hi + 1, step))
    return out


class HarnessError(RuntimeError):
    pass


def run_harness(harness, rom, frames, keys, prefix, *, frame_read='',
                state=None, sav=None, save=None, timeout=60, choice_capture=None,
                dialogue_capture=False):
    """Return fresh, complete evidence or fail; never consume a prior run's files."""
    if type(frames) is not int or frames <= 0 or timeout <= 0:
        raise ValueError('frames 和 timeout 必须为正')
    events = expand_keys(keys, frames)
    for path in (rom, state, sav):
        if path and not Path(path).is_file():
            raise HarnessError(f'输入文件不存在：{path}')
    if len(frame_read) >= 1024:
        raise ValueError('采样规格超过 harness 的 1024 字节缓冲')
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    # Inherited debug hooks can mutate RAM, inject keys, change BIOS or load saves.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GBARUN_')}
    env['GBARUN_NO_AUTOSAVE'] = '1'
    if state:
        env['GBARUN_STATE'] = os.path.abspath(state)
    if sav:
        env['GBARUN_SAVFILE'] = os.path.abspath(sav)
    if frame_read:
        env['GBARUN_FRAME_READ'] = frame_read
    for ext in (*RAM_SIZES, 'ppm', 'log'):
        prefix.with_suffix(prefix.suffix + '.' + ext).unlink(missing_ok=True)
    # Each invocation writes into its own directory, even when callers share a name.
    with tempfile.TemporaryDirectory(prefix='.run-', dir=prefix.parent) as tmp:
        fresh = Path(tmp) / 'snapshot'
        if choice_capture is not None:
            captures = Path(tmp) / 'choices'
            captures.mkdir()
            env['GBARUN_CHOICE_DIR'] = str(captures.resolve())
            if dialogue_capture:
                env['GBARUN_CAPTURE_DIALOGUE'] = '1'
        env['GBARUN_RAMSNAP'] = f'{frames - 1}:{fresh}'
        env['GBARUN_SHOT'] = str(fresh) + '.ppm'
        if save:
            env['GBARUN_SAVE'] = str(fresh) + '.ss1'
        cmd = [str(harness), str(rom), str(frames), str(fresh)] + events
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            def as_text(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
            out = as_text(exc.stdout) + as_text(exc.stderr)
            Path(str(prefix) + '.log').write_text(out + '\nTIMEOUT\n', encoding='utf-8')
            raise HarnessError(f'harness 超时（{timeout} 秒）') from exc
        except OSError as exc:
            raise HarnessError(f'无法运行 harness：{exc}') from exc
        out = proc.stdout + proc.stderr
        Path(str(prefix) + '.log').write_text(out, encoding='utf-8')
        evidence = {}
        for ext in (*RAM_SIZES, 'ppm'):
            path = Path(str(fresh) + '.' + ext)
            if path.is_file():
                evidence[ext] = path.read_bytes()
                Path(str(prefix) + '.' + ext).write_bytes(evidence[ext])
        if proc.returncode:
            raise HarnessError(f'harness 退出码 {proc.returncode}：{out[-1500:]}')
        for label, path in (('state', state), ('save', sav)):
            if path and f'[{label}] loaded ' not in out:
                raise HarnessError(f'harness 未能加载 {label}：{out[-1500:]}')
        for ext, size in RAM_SIZES.items():
            if len(evidence.get(ext, b'')) != size:
                raise HarnessError(f'{ext} 快照缺失或长度错误（应为 {size} 字节）')
        header = b'P6\n240 160\n255\n'
        shot = evidence.get('ppm', b'')
        if not shot.startswith(header) or len(shot) != len(header) + 240 * 160 * 3:
            raise HarnessError('截图缺失或内容不完整')
        if save:
            checkpoint = Path(str(fresh) + '.ss1')
            if '[state] wrote ' not in out or not checkpoint.is_file() or not checkpoint.stat().st_size:
                raise HarnessError('harness 未能保存即时存档')
            Path(save).write_bytes(checkpoint.read_bytes())
        if choice_capture is not None:
            match = re.search(r'^\[choices\] captured (\d+)$', out, re.MULTILINE)
            if match is None:
                raise HarnessError('harness 不支持选项截图；请运行 make dbg 重新编译')
            snapshots = list(captures.glob('frame-*.bin'))
            if len(snapshots) != int(match[1]):
                raise HarnessError('选项截图记录不完整')
            for snapshot in snapshots:
                ppm = snapshot.with_suffix('.ppm').read_bytes()
                if snapshot.stat().st_size != 0x60400 or not ppm.startswith(header) or len(ppm) != len(shot):
                    raise HarnessError('选项截图或状态长度错误')
            shutil.copytree(captures, choice_capture)
        return evidence, out
