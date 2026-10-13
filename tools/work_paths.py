"""Fixed locations for generated artifacts; a run owns one replaceable directory."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import html
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'work'
AUTOPLAY = WORK / 'autoplay'
REVALIDATE = WORK / 'revalidate'
VIDEO = WORK / 'video'
DEBUG_PLAY = WORK / 'dbg/play'


OUTPUTS = (
    ('推理题审核', 'autoplay/inference-review/index.html', '仅保留推理题，包含滚动页'),
    ('通关结果', 'autoplay/result.json', 'CLEAR 才代表完整路线通过'),
    ('通关画面', 'autoplay/ending.png', '本次运行的末帧'),
    ('自动游玩日志', 'autoplay/run.log', '运行失败时也保留'),
    ('游玩断点与证据', 'autoplay/checkpoints/', 'segment-0001 起连续编号'),
    ('新 ROM 候选路线', 'revalidate/route.candidate.json', '必须再经过 autoplay 严格验收'),
    ('候选验证日志', 'revalidate/run.log', '重新验证已有按键路线'),
    ('路线导出', 'route-export/route.candidate.json', '从探索记录生成，尚未经回放验收'),
    ('录像', 'video/playthrough.mp4', '默认第 1–2 天，可指定结束段号'),
    ('动态检查证据', 'dyn/', '按检查规格中的用例名称组织'),
    ('单段调试', 'dbg/play/', 'play.ss1 与 play.ppm_final.ppm'),
    ('检查与导出报告', 'reports/', '每种工具对应固定文件名'),
    ('调试渲染图片', 'images/', '每种工具对应固定文件名'),
    ('字形表图片', 'glyph-sheets/', 'sheet-0001.png 起连续编号'),
    ('翻译批次', 'tm/batches/', 'b001.tsv 起连续编号'),
    ('测试日志', 'logs/', 'unit.log、dynamic.log、verify.log'),
    ('ROM 备份', 'backup/', '固定中文 ROM 文件名'),
)


def write_index():
    WORK.mkdir(parents=True, exist_ok=True)
    rows = ''.join(f'<tr><td><a href="{html.escape(path)}">{label}</a></td>'
                   f'<td><code>{path}</code></td><td>{note}</td></tr>'
                   for label, path, note in OUTPUTS)
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>项目输出</title><style>body{font:16px system-ui;margin:40px;'
            'line-height:1.6}td{padding:8px 16px;border-bottom:1px solid #ddd}'
            'code{font-size:14px}</style><h1>项目输出</h1>'
            '<p>各工具始终使用下列固定位置。尚未运行的工具可能没有输出。'
            '自动游玩默认从头更新；make autoplay AUTOPLAY_ARGS="--resume" 续跑。</p>'
            '<table>' + rows + '</table></html>')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=WORK,
                                     prefix='.index-', delete=False) as temporary:
        temporary.write(page)
    Path(temporary.name).replace(WORK / 'index.html')


def output_file(name):
    """Prepare a code-defined artifact filename relative to work/."""
    path = WORK / name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_index()
    return str(path)


class Tee:
    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        self.stream.write(text)
        self.log.write(text)
        self.log.flush()
        return len(text)

    def flush(self):
        self.stream.flush()
        self.log.flush()


@contextmanager
def fixed_run(directory, *, resume=False, inputs=()):
    """Serialize writers, protect inputs, and retain only this tool's latest run.

    Callers supply code-defined locations, never command-line output paths.
    Locks live outside the replaced directory and are released on process exit.
    A failed run keeps its log and checkpoints for inspection or resume.
    """
    directory = Path(directory)
    relative = directory.relative_to(WORK)
    if not relative.parts or '..' in relative.parts:
        raise ValueError('运行目录必须是 work/ 下的工具专用目录')
    for ancestor in (directory, *directory.parents):
        if ancestor.is_symlink():
            raise ValueError(f'运行目录不能经过符号链接：{ancestor}')
        if ancestor == WORK:
            break
    for source in inputs:
        if source and Path(source).resolve().is_relative_to(directory.resolve()):
            raise ValueError(f'输入位于即将更新的输出目录中，请先移出：{source}')
    locks = WORK / '.locks'
    locks.mkdir(parents=True, exist_ok=True)
    write_index()
    with (locks / ('-'.join(relative.parts) + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'该工具正在运行，请勿同时更新：{directory}') from None
        try:
            if not resume and directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / 'run.log').open('a' if resume else 'w', encoding='utf-8') as log:
                with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
                    try:
                        yield directory
                    except BaseException as exc:
                        print(f'运行未完成：{exc}', file=sys.stderr)
                        raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == '__main__':
    import argparse
    argparse.ArgumentParser(description='生成固定入口 work/index.html；无需输出目录参数').parse_args()
    write_index()
    print(WORK / 'index.html')
