# dApp Manager — dappman (قراءة فقط)

أداة واحدة متكاملة: قائمة مراقبة ← فحص دوري ← قاعدة SQLite ← تنبيهات ← لوحة ويب محلية ← تصدير CSV.
Python 3.10+ فقط، بدون مكتبات خارجية، بدون مفتاح API، بدون حساب. Read-only: لا توقيع ولا تحريك أموال ولا نشر عقود.
العميل يرفض برمجيًا أي أداة غير قراءة.

## التشغيل (من جذر المستودع، Python 3.10+)
```bash
git clone https://github.com/fouadbehtaher/dapp-manager && cd dapp-manager
python3 -m dappman add 0xdAC17F958D2ee523a2206206994597C13D831ec7 --label USDT --kind contract
python3 -m dappman add 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 --kind wallet --label sample
python3 -m dappman scan          # الأول = خط أساس، الثاني فصاعدًا = تنبيهات
python3 -m dappman watch --every 300
python3 -m dappman alerts
python3 -m dappman export --out out
python3 -m dappman serve --port 8787   # http://127.0.0.1:8787
python3 -m unittest discover -s tests -v
```
الشبكات: 1 (Ethereum) و137 (Polygon) و42161 (Arbitrum) عبر `--chain`.

## التنبيهات
معاملة جديدة، معاملة فاشلة، قيمة كبيرة (`--big`)، تحويلات ERC-20 جديدة، تغيّر حالة التوثيق، تغيّر hash الكود المصدري (critical)، تغيّر implementation للـproxy (critical)، فشل فئة بيانات كانت تعمل.

## الحدود (صريحة)
- آخر 50 سجلًا لكل فئة في كل فحص، وليس السجل الكامل. تشغيل `watch` يراكم السجل من لحظة البدء فقط.
- نقطة Etherscan العامة بدون مفتاح، حدّ معدلها غير موثّق وقد يتغير. الأخطاء تظهر كأخطاء ولا تتحول لأصفار.
- لا أرصدة NFT/ERC-20 الحالية (تحتاج نقطة PRO). القيم المعروضة تحويلات وليست ملكية.
- ليست تدقيقًا أمنيًا: تنبيه تغيّر الكود/الـproxy مؤشر مراقبة فقط.
- لا إشعارات خارجية (Telegram/Email) بعد. اللوحة محلية على 127.0.0.1 ولا تُنشر.
- المسح الفعلي لا يتحقق من ملكية أي عقد عام؛ العناوين التجريبية عامة ولا علاقة لأصحابها.

MIT License.

---

## سكربتات النماذج الأولية (src/)

السكربتات الأولى ما زالت في `src/` كما رُفعت سابقًا (تقرير محفظة وتقرير عقد بصيغة DOCX/PDF). الأداة المتكاملة هي `dappman/` أعلاه.
