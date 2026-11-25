# مدير التطبيقات اللامركزية — dApp Manager (قراءة فقط)

أدوات Python لمتابعة العقود والمحافظ على شبكات EVM عبر خدمة **Etherscan Keyless MCP** العامة —
بدون مفتاح API وبدون حساب وبدون أي صلاحية كتابة.

## ماذا تفعل (مجربة فعليًا)

| الأداة | الوظيفة |
|---|---|
| `src/ethscan_demo.py` | تقرير محفظة: رصيد أصلي + سجلات معاملات عادية/داخلية وتحويلات ERC-20/721/1155 عبر شبكات، كملفات CSV و`summary.json` |
| `src/build_docx.py` | تقرير عربي PDF/DOCX بصفحة واحدة من مخرجات الأداة السابقة |
| `src/dapp_monitor.py` | لوحة عقد ذكي: بيانات الإنشاء، حالة التحقق من الكود، آخر المعاملات وتحويلات الرموز |
| `src/build_monitor_docx.py` | تقرير عربي PDF/DOCX بصفحة واحدة من مخرجات لوحة العقد |

كل الأدوات **قراءة فقط**: لا توقيع، لا تحريك أموال، لا تغيير عقود، لا إنشاء مفاتيح API.

## الاستخدام

```bash
python3 src/ethscan_demo.py <عنوان_عام> [اسم_مستعار] [outdir]
python3 src/build_docx.py <outdir>
python3 src/dapp_monitor.py <chain_id> <عنوان_العقد> [outdir]
python3 src/build_monitor_docx.py <outdir>
```

يعتمد فقط على مكتبات Python القياسية، وعلى LibreOffice اختياريًا لتحويل DOCX إلى PDF.

## الحدود (مهم)

- البيانات عبر نقطة النهاية العامة المحدودة المعدل من Etherscan (حوالي ٢٠٠ طلب صريحًا في اختباراتنا، والحد قد يتغير) — وليست بديلًا عن خطة API مدفوعة.
- الأرصدة الحالية لرموز ERC-20/NFT غير متاحة بدون مفتاح (نقطة PRO)؛ أي تقدير من سجل التحويلات **استنتاج قد يخطئ** وليس إثبات ملكية.
- العينات في المستودع عناوين عامة (مثل vitalik.eth) لأغراض التوضيح فقط — لا ملكية ولا علاقة بأصحابها.
- التغطية: الشبكات المتاحة في الطبقة المجانية من Etherscan؛ بعض الشبكات (مثل BNB Chain وBase) تتطلب خطة مدفوعة.

## English

Read-only EVM wallet/contract monitoring tools over the Etherscan keyless MCP endpoint (no API key, no account). Wallet report (CSV + one-page Arabic PDF) and smart-contract panel (creation info, verification status, recent activity). Everything is read-only: no signing, no fund movement, no contract changes. See the Arabic sections above for details; public sample addresses are for illustration only.
