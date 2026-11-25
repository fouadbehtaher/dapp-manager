#!/usr/bin/env python3
"""Build a clean one-page Arabic DOCX wallet report from cached CSVs, no API calls.
LibreOffice converts it to PDF for delivery proof."""
import csv, json, os, sys, time
csv.field_size_limit(10**8)
import docx
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT = sys.argv[1]
S = json.load(open(os.path.join(OUT, 'summary.json')))
ADDR = S['address']
CH = S['chains'][0]
CHAIN, SYM = CH['cname'], CH['sym']
BAL = CH['balance_native'] or 'غير متاح'

def count(name):
    p = os.path.join(OUT, f"{name}_{CHAIN.replace(' ', '')}.csv")
    if not os.path.exists(p): return 0
    with open(p) as f: return sum(1 for _ in csv.DictReader(f))

labels = [('معاملات عادية', 'normal'), ('معاملات داخلية', 'internal'),
          ('تحويلات رموز ERC-20', 'erc20'), ('تحويلات NFT بمعيار ERC‑721', 'erc721'),
          ('تحويلات NFT بمعيار ERC‑1155', 'erc1155')]
TRUNC = 'غير مكتمل — تجاوز حد طلبات الديمو'
STAMP = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(os.path.getmtime(os.path.join(OUT, 'normal_Ethereum.csv'))))

d = docx.Document()
for sec in d.sections:
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(1.5)

style = d.styles['Normal']
style.font.name = 'Noto Sans Arabic'
style.font.size = Pt(14)
style.element.rPr.rFonts.set(qn('w:cs'), 'Noto Sans Arabic')
style.element.rPr.sz.set(qn('w:val'), '28')  # 14pt complex script

def rtl(p):
    pPr = p._p.get_or_add_pPr()
    bidi = OxmlElement('w:bidi'); bidi.set(qn('w:val'), '1'); pPr.append(bidi)
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

def para(text, bold=False, size=14, mono=False, color=None):
    p = d.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.font.size = Pt(size)
    szcs = OxmlElement('w:szCs'); szcs.set(qn('w:val'), str(size * 2)); r._r.get_or_add_rPr().append(szcs)
    if mono:
        r.font.name = 'Courier New'
        rPr = r._r.get_or_add_rPr()
        rf = rPr.find(qn('w:rFonts'))
        rf.set(qn('w:ascii'), 'Courier New'); rf.set(qn('w:hAnsi'), 'Courier New')
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        return p
    if color: r.font.color.rgb = RGBColor(*color)
    rtl(p)
    return p

para('تقرير محفظة — ديمو قراءة فقط', bold=True, size=18)
para('العنوان (عينة عامة للتوضيح — vitalik.eth):', size=11)
para(ADDR, mono=True, size=10)
para('وقت البيانات:', size=11)
para(STAMP, mono=True, size=10)
para(f'شبكة {CHAIN} — الرصيد الأصلي: {BAL} {SYM}', bold=True)

t = d.add_table(rows=1, cols=3)
t.style = 'Table Grid'
t.alignment = WD_TABLE_ALIGNMENT.RIGHT
t.autofit = False
tblPr = t._tbl.tblPr
layout = OxmlElement('w:tblLayout'); layout.set(qn('w:type'), 'fixed'); tblPr.append(layout)
bidi = OxmlElement('w:bidiVisual'); tblPr.append(bidi)
tw = OxmlElement('w:tblW'); tw.set(qn('w:w'), str(17*567)); tw.set(qn('w:type'), 'dxa'); tblPr.append(tw)
grid = t._tbl.tblGrid
for gc in list(grid): grid.remove(gc)
for wcm in (7.5, 3.0, 6.5):
    gc = OxmlElement('w:gridCol'); gc.set(qn('w:w'), str(int(wcm*567))); grid.append(gc)
widths = [Cm(7.5), Cm(3.0), Cm(6.5)]
hdr = t.rows[0].cells
for i, h in enumerate(['الفئة', 'السجلات المجلوبة', 'حالة الاكتمال']):
    c = hdr[i]; c.width = widths[i]
    r = c.paragraphs[0].add_run(h); r.bold = True
    rtl(c.paragraphs[0])
for lab, key in labels:
    cells = t.add_row().cells
    vals = [lab, str(count(key)), TRUNC]
    for i, v in enumerate(vals):
        cells[i].width = widths[i]
        p = cells[i].paragraphs[0]
        p.add_run(v)
        rtl(p)

para('الحالة: السجل المجلوب غير مكتمل في كل الفئات (العنوان العام المختار نشط جدًا وتجاوز حد طلبات الديمو). '
     'الصفوف المجلوبة فعليًا محفوظة كاملة في ملفات CSV المرافقة. في محفظة عادية النشاط، تكتمل كل الفئات عادة.', size=12)
para('تنبيه NFT: أي عدد للرموز المملوكة يكون استنتاجًا من سجل التحويلات فقط، بدون تحقق مباشر من الملكية الحالية '
     '(ownerOf/balanceOf)، وقد يزيد أو ينقص عن الواقع. لذلك لا يعرض هذا التقرير عددًا للملكية الحالية، '
     'ولا يمثل إثبات ملكية ولا تقييمًا ماليًا.', size=12, bold=True)
para('قراءة فقط عبر خدمة Etherscan العامة المحدودة المعدل. حد صريح ٢٠٠ طلب في الديمو ولم تظهر أخطاء تجاوز معدل. '
     'لا مفاتيح حساب، لا توقيع، لا معاملات.', size=10)

d.save(os.path.join(OUT, 'report.docx'))
print('docx written')
