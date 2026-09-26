# Zpix (最像素) — 新增字形用的点阵字库

* 文件：`zpix.bdf`（v3.3.0，BDF 点阵格式，22,238 字形）
* 来源：<https://github.com/SolidZORO/zpix-pixel-font>（Release v3.3.0）
* 规格：12px（11px 字形 + 1px 间距）；CJK 字形 **11×11**，正好等于本作的设计框
* 覆盖：本项目新增的 1369 个汉字全部包含

## 为什么用它

原来的做法是把 18px 轮廓字体缩放進 11×11 再二值化，笔画会断、粗细不匀，
实机看着发糊。改为直接取点阵位图后，每个像素都落在网格上。

## 授权（重要）

Zpix 采用「个人 / 教育免费、商业收费」的授权模式（README 标价
USD 1000 / 单一商业产品）。本汉化是免费同人作品，属个人用途；
**若要商用请更换为 OFL 授权的点阵字库**（例如 Ark Pixel 方舟像素）。

`tools/pixelfont.py` 只依赖 BDF 的 `BBX` / `BITMAP` 字段，换字库只需替换本目录的
`.bdf` 文件并调整 `pixelfont.DEFAULT` 路径。

## 用法

```bash
python3 tools/pixelfont.py --png sample.png --text "汉字测试"   # 预览
python3 tools/font_compare.py --out cmp.png --text "一行中文"    # 与原方案对比
python3 tools/build_rom.py --out out.gba                       # 打包时自动使用
python3 tools/build_rom.py --outline-font                      # 回退到轮廓字体
```
