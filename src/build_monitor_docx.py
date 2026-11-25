#!/usr/bin/env python3
"""Render dApp monitor DOCX from monitor_summary.json. No API calls."""
import json, os, sys
import docx
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT = sys.argv[1]
secs = json.load(open(os.path.join(OUT, 'monitor_summary.json')))

d = docx.Document()
for sec in d.sections:
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
style = d.styles['Normal']
style.font.name = 'Noto Sans Arabic'
style.font.size = Pt(13)
style.element.rPr.rFonts.set(qn('w:cs'), 'Noto Sans Arabic')

def rtl(p):
    pPr = p._p.get_or_add_pPr()
    b = OxmlElement('w:bidi'); b.set(qn('w:val'), '1'); pPr.append(b)
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

def para(text, bold=False, size=13, mono=False):
    p = d.add_paragraph()
    r = p.add_run(text); r.bold = bold; r.font.size = Pt(size)
    szcs = OxmlElement('w:szCs'); szcs.set(qn('w:val'), str(size * 2)); r._r.get_or_add_rPr().append(szcs)
    if mono:
        r.font.name = 'Courier New'
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        return p
    rtl(p)
    return p

para('لوحة إدارة العقود — شريحة أولى (قراءة فقط)', bold=True, size=17)
para('العينة: عقد SHFL العام من رابطك السابق — مثال توضيحي فقط، لا يعني ملكيتنا أو إدارتنا للعقد.', size=11)
for s in secs:
    para(f"شبكة {s['chain']}", bold=True, size=15)
    para(s['addr'], mono=True, size=10)
    if s.get('name'):
        para(f"اسم العقد: {s['name']} — المصدر موثّق (Verified): {'نعم' if s.get('verified') else 'لا'}", bold=True)
    if s.get('creator'):
        para('أنشئ بواسطة:', size=11)
        para(s['creator'], mono=True, size=10)
        para(f"تاريخ الإنشاء: {s['created']}", size=12)
    para(f"آخر {s.get('recent_tx', 0)} معاملة على العقد — أحدثها: {s.get('latest_tx', '?')} — الفاشلة منها: {s.get('failed_recent', 0)}")
    if 'recent_tt' in s:
        para(f"آخر {s['recent_tt']} تحويل رموز — أحدثها: {s['latest_tt']}")
    if s.get('errors'):
        para('أقسام لم تُجلب: ' + ' | '.join(s['errors']), size=11)
para('قراءة فقط عبر خدمة Etherscan العامة المحدودة المعدل. لا مفاتيح حساب، لا توقيع، لا معاملات. النشاط المعروض أحدث ٢٥ سجلًا فقط، مش السجل الكامل.', size=10)
d.save(os.path.join(OUT, 'monitor.docx'))
print('monitor.docx written')
