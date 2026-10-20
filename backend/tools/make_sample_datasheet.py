"""生成一份用于端到端验证的合成规格书 PDF（非真实文档）。"""

import pathlib
import sys

import pymupdf

LINES = [
    "MZB1001T02  微型风扇（空气泵）",
    "Technical Specification",
    "",
    "项目                  规格",
    "输入电压              11.5~21 Vdc",
    "驱动谐振频率          24~27 kHz",
    "空气流量(1)           ≥0.7 L/min",
    "静压(1)               >=1.42 kPa",
    "工作温度范围          0~70 ℃",
    "外形尺寸              20×20×1.85 mm",
    "重量                  1.4 g",
    "备注：流量与静压均在 15Vpp 条件下测得。",
    "(1) 典型值，随负载变化。",
]

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "data/_test_datasheet.pdf")
out.parent.mkdir(parents=True, exist_ok=True)

doc = pymupdf.open()
page = doc.new_page()
y = 80
for line in LINES:
    page.insert_text((60, y), line, fontsize=11, fontname="china-s")
    y += 20
doc.save(str(out))
doc.close()
print(f"saved {out} ({out.stat().st_size} bytes)")
