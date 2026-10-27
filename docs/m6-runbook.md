# M6 — haqiqiy Vissim'da tekshirish: loyiha egasi uchun yo'riqnoma

Bu hujjat PLAN.md 10.3-bo'lim (1–6-bandlar), 11-bo'lim M6 qatori va 13-bo'lim "M6" promptini **bitta sessiyada**
bajarish uchun. Hamma narsa tayyorlab qo'yilgan: `scripts/probe_com_names.py`, `tests/real/*`,
`docs/test-report-v1.md` shabloni. Hozir (2026-10-27) ishga tushirib bo'lmaydi: kompyuterdagi yagona litsenziya —
**PTV Vissim (Demo)**, unda COM moduli yo'q (docs/preflight-report.md).

Taxminiy vaqt: tayyorgarlik 15 daqiqa, probe 5–10 daqiqa, avtomatik testlar 30–90 daqiqa (eng uzuni — 5 × 3600 s
stress testi), qo'lda bajariladigan qadamlar 15 daqiqa, natijalarni yozish 30 daqiqa.

## 0. Xavfsizlik qoidalari (o'qib chiqing)

- Skript va testlar **faqat o'zlari ochgan** Vissim'ni (`Vissim260.exe -Embedding`) yopadi yoki o'ldiradi: PID,
  jarayon nomi va ishga tushgan vaqti tekshiriladi. Siz o'zingiz ochgan Vissim oynasiga hech qachon tegilmaydi.
- Testlar faqat **nusxa** bilan ishlaydi: `COM Basic Commands.inpx` vaqtinchalik papkaga ko'chiriladi, asl fayl
  o'zgarmaydi.
- Bitta o'rinli (single-seat) litsenziyada siz ochgan Vissim litsenziyani band qilib turadi va COM orqali ochilgan
  nusxa litsenziya dialogini ko'rsatadi. Shuning uchun **M6 boshlanishidan oldin barcha Vissim oynalarini o'zingiz
  yoping** (saqlab).
- Task Manager'da faqat yo'riqnomada ko'rsatilgan PID'ni yoping. "Command line" ustunida `-Embedding` bo'lmagan
  Vissim — sizniki, unga tegmang.

## 1. Tayyorgarlik

1. **Litsenziya.** COM moduli bor to'liq litsenziyani (yoki PTV Hub obunasini) faollashtiring va Vissim'ning
   litsenziya oynasida uni **default** qilib saqlang — Vissim ishga tushganda tanlash dialogi chiqmasligi kerak.
   Tekshirish: Vissim'ni oddiy oching → Help → License: COM moduli ro'yxatda bo'lsin. Keyin Vissim'ni yoping.
2. **Repozitoriya.** PowerShell'da:
   ```powershell
   cd D:\PTV-Vissim
   git switch m6-real-prep        # yoki M6 ishlari uchun shu branch'dan yangi branch
   $env:PYTHONUTF8 = "1"
   .\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider -q
   ```
   Kutilgan natija: hammasi yashil (`... passed, 1 skipped, 12 deselected`). 12 ta `deselected` — `tests/real`
   testlari, ular standart ishga tushirishda bajarilmaydi.
3. **COM smoke test** (M0 dagi tekshiruvni qaytarish):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -ComSmoke
   ```
   Kutilgan natija: docs/preflight-report.md avtomatik bo'limidagi "COM smoke test" ostida
   `- OK: Dispatch ... s, Net.Links.Count = ...` qatori; "PROBLEM: Only Demo/Student ..." qatori yo'qolgan. Litsenziya dialogi chiqsa — 1-qadamga qayting,
   davom etmang.
4. Hamma Vissim oynalari yopilganini Task Manager → "Details" yorlig'ida tekshiring (`Vissim260.exe` yo'q).

## 2. A qadam — COM nomlarini tekshirish (PLAN 10.3, 2-band)

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\probe_com_names.py --i-have-a-com-license
```

Skript nima qiladi: o'zining Vissim nusxasini COM orqali ochadi (Job Object bilan), namunaviy tarmoqning nusxasini
yuklaydi, docs/com-names.md jadvalidagi har bir nomni o'qiydi (xavfsiz bo'lsa o'sha qiymatni qayta yozib, qayta
o'qiydi), so'ng tajribalarni bajaradi:

| Tajriba | Savol |
|---|---|
| `break_at_n` | `SimBreakAt=120` → `RunContinuous()` aynan 120 s da to'xtaydimi? To'xtamasa — `IsSimBreakAtActive=True` bilan qayta urinadi |
| `signal_outside_run` | Simulyatsiya yo'q paytda `SigState` yozilsa nima bo'ladi, `ContrByCOM` o'zgaradimi |
| `signal_while_paused` | Pauzada `SigState="GREEN"` va `SigState=1` yoziladimi, `ContrByCOM` avtomatik `True` bo'ladimi, `False` bilan qaytariladimi |
| `stale_break` | `SimSec`=120 da `SimBreakAt=60` (o'tib ketgan) qo'yilsa `RunContinuous()` nima qiladi |
| `break_disable_runs_to_end` | `SimBreakAt=0` (`VISSIM_BREAK_DISABLE_VALUE`) bilan run oxirigacha boradimi; run tugagandan keyin `SimSec` nima |
| `single_step_and_stop` | `RunSingleStep()` bitta qadam yuradimi; `Stop()` dan keyin `SimSec` |
| `item_by_key_string` | `Net.Links.ItemByKey("1")` (satr kalit) ishlaydimi |
| jarayonlar | Vissim'ning bola jarayonlari Job Object ichidami (`CREATE_BREAKAWAY_FROM_JOB` kerakmi) |
| `Exit()` | `vissim_close` qaysi usul bilan yopdi (`exit` / `terminate` / `kill`) va qancha vaqt ketdi |

Kutilgan natija: jadval (nom | status | kuzatilgan qiymat), oxirida `raw results: ...\probe-results.json`.
Chiqish kodi: `0` — hamma nom ishladi; `1` — ba'zi nomlar `failed` (jadvalga qarang); `2` — rad etildi (flag yo'q);
`3` — Vissim ochilmadi yoki tarmoq yuklanmadi (masalan, litsenziya dialogi).

Natija sizni qoniqtirsa, hujjatlarni yangilang:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\probe_com_names.py --i-have-a-com-license --update
```

`--update` docs/com-names.md dagi "Holat" ustunini (`verified` / `rejected`) va probe bloki xulosasini yangilaydi
hamda docs/test-report-v1.md oxiriga xom natijalarni (jadval + JSON) qo'shadi. Natijani `git diff docs` bilan ko'rib
chiqing.

## 3. B qadam — avtomatik haqiqiy testlar (PLAN 10.3, 1–6-bandlar)

Barcha avtomatik testlar (qo'lda qadamlarsiz):

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider tests\real -m vissim --vissim-com-license `
    -rA --vissim-results docs\m6-real-results.jsonl
```

`--vissim-com-license` siz har bir test darhol `skipped` bo'ladi (hech narsa ishga tushmaydi). Bitta bandni alohida
ishga tushirish uchun faylni ko'rsating, masalan `tests\real\test_real_smoke.py`.

| Band | Fayl / test | Nima qiladi | Kutilgan natija |
|---|---|---|---|
| 1. Smoke | `test_real_smoke.py` | connect → yuklash → `Links.Count` → 60 s simulyatsiya → close | `PASSED`; close'dan keyin Vissim jarayoni yo'q |
| 2. Nomlar | `test_real_com_names.py` | `probe_com_names.py` ni subprocess sifatida ishga tushiradi (hujjatlarni o'zgartirmaydi) | `PASSED`: server tayanadigan nomlar (LoadNet, SaveNetAs, Exit, SimPeriod, SimRes, SimSec, SimBreakAt, RandSeed, break o'chirish qiymati, ...) `ok` |
| 3. Break | `test_real_break_semantics.py` | xom COM orqali (`vissim_exec_python`, faqat shu server uchun yoqilgan) `SimBreakAt=120` va `SimBreakAt=0`; eski `SimBreakAt=250` qolgan holatda job; `pause_at=[200]` → signal holati → resume | 3 ta test `PASSED` |
| 4. Stress | `test_real_stress.py` | 5 run × 3600 s; `percent` monoton; `vissim_health` < 1 s; `SimulationRuns` da 5 ta urug'; alohida testda `stop` bir bo'lak (60 s) ichida | 2 ta test `PASSED` (birinchisi uzoq — `--vissim-stress-timeout` standart 3600 s) |
| 5. Nosozlik | `test_real_failures.py` | Vissim'ni o'ldirish (avtomatik) → aniq xato → `vissim_restart`; server o'ldirilsa Vissim yopiladi (Job Object); stdin yopilsa ≤ 300 ms da chiqish | 3 ta `PASSED`, dialog testi `SKIPPED` (u C qadamda) |
| 6. Kodirovka | `test_real_encoding.py` | `Samarqand_ko'cha_Самарқанд\tarmoq_ўзбек.inpx` ni yuklash, saqlash, CSV eksport | `PASSED`, yuklashda `non-ASCII path` ogohlantirishi |

Test oxirida pytest "M6 observations" bo'limini chiqaradi — har bir test kuzatgan qiymatlar (JSON qatorlar). Ular
`docs\m6-real-results.jsonl` ga ham yoziladi.

Test muvaffaqiyatsiz bo'lsa: xato matnini, "M6 observations" qatorlarini va server log'ini (test vaqtinchalik
papkasidagi `logs\vissim-mcp.log`, yo'li xato matnida) saqlang va test hisobotiga yozing. Hech narsani qo'lda
"tuzatib" testni qayta o'tkazmang — avval sababini yozing.

## 4. C qadam — qo'lda bajariladigan qadamlar (PLAN 10.3, 5-band)

Ikki test sizning harakatingizni kutadi. Ularni `-s` (konsolga chiqish) va `--vissim-manual` bilan ishga tushiring:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider tests\real\test_real_failures.py `
    -m vissim --vissim-com-license --vissim-manual -s -rA --vissim-results docs\m6-real-results.jsonl
```

**C1. Vissim'ni Task Manager'dan o'ldirish** (`test_killed_vissim_gives_a_clear_error_and_restart_recovers`).
Test "QO'LDA QADAM" blokini va PID'ni chiqaradi.
1. `Ctrl+Shift+Esc` → "Details" yorlig'i.
2. Ko'rsatilgan PID'li `Vissim260.exe` ni toping (ustunlar ichida "Command line" bo'lsa, unda `-Embedding` bor).
3. O'ng tugma → "End task" → tasdiqlang.
4. Konsolga qayting va Enter bosing.

Kutilgan natija: keyingi chaqiruv `COM_ERROR` yoki `NOT_CONNECTED` qaytaradi, xabarida `vissim_restart` maslahati
bor; `vissim_restart` yangi Vissim ochadi va tarmoq qayta yuklanadi; test `PASSED`.

**C2. "Save changes?" dialogi** (`test_modal_dialog_gives_timeout_hung_and_restart_recovers`). Test Vissim'ni
ochadi, tarmoqni yuklaydi va bitta havola nomini o'zgartiradi (tarmoq "o'zgartirilgan" holatda).
1. Test ochgan Vissim oynasini toping (sarlavhada `COM Basic Commands`, PID test xabarida).
2. Oynaning yuqori o'ng burchagidagi **X** tugmasini bosing.
3. "Save changes?" / "Сохранить изменения?" dialogi chiqadi — **unga javob bermang**, ochiq qoldiring.
4. Konsolga qayting va Enter bosing.

Kutilgan natija: `vissim_info` 20 s dan keyin `TIMEOUT_HUNG` qaytaradi (`VISSIM_CALL_TIMEOUT_S=20` shu test uchun),
`vissim_health` da `busy_seconds > 0`; `vissim_restart` osilib qolgan Vissim'ni o'ldiradi va yangisini ochadi;
test `PASSED`. Agar COM chaqiruvi dialogga qaramay javob bersa, test `FAILED` bo'ladi va "the dialog did not block
COM" deydi — bu ham muhim natija: test hisobotiga aynan shunday yozing.

**C3. Claude Code'ni yopish** (PLAN 10.3, 5-band, oxirgi qism; avtomatik ekvivalenti —
`test_killed_server_leaves_no_vissim`). Buni haqiqiy Claude Code bilan bir marta tekshiring:
1. Sinov papkasida (masalan `D:\VissimProjects\m6`) PLAN.md 9.1 dagi `.mcp.json` ni yarating
   (`command` — `D:\PTV-Vissim\.venv\Scripts\python.exe`, `VISSIM_ALLOWED_DIRS` — shu papka).
2. Shu papkada Claude Code'ni oching, `/mcp` → `vissim ✔` va 22 ta tool ko'rinishini tekshiring.
3. Claude'dan `vissim_connect` ni chaqirishni so'rang; Task Manager'da `Vissim260.exe -Embedding` paydo bo'ladi.
4. Claude Code oynasini yoping (yoki terminalda `/exit`).
5. 10 s ichida Task Manager'da shu `Vissim260.exe` yo'qolishi kerak.

## 5. Natijalarni yozish

1. **docs/test-report-v1.md** — har bir 10.3 bandi bo'limida:
   - `Holat` qatorini almashtiring: `PASSED` / `FAILED` / `PASSED (izoh bilan)`, sana va Vissim versiyasi;
   - `Dalil` ga pytest natijasi (`-rA` xulosasi), tegishli `m6-real-results.jsonl` qatorlari, kerak bo'lsa skrinshot
     nomi;
   - topilgan farq bo'lsa — `Topilmalar` qatoriga: nima kutilgan, nima bo'ldi, qaysi o'zgarish qilindi.
2. **docs/com-names.md** — `probe_com_names.py --update` yangilaydi. "M6 da tekshiriladigan jarayon
   xatti-harakatlari" jadvaliga natijalarni qo'lda yozing (Job Object, `Exit()` javobi, `ItemByKey` kalit turi).
   Yakunda har bir nom `verified`, `rejected` yoki `configurable` holatida bo'lishi kerak (PLAN 10.4).
3. **Topilmalarni tuzatish** (M6 DoD: "topilgan farqlar kodda/konfiguratsiyada tuzatilgan"):

   | Topilma | Choralar |
   |---|---|
   | `SimBreakAt=0` run'ni oxirigacha olib bormaydi | `VISSIM_BREAK_DISABLE_VALUE` ni o'zgartirish (masalan, period'dan katta qiymat) va B qadamni qayta o'tkazish |
   | `SimBreakAt` faqat `IsSimBreakAtActive=True` bilan ishlaydi | `jobs.py` ga o'zgartirish kerak (job boshida `IsSimBreakAtActive=True`) — kod o'zgarishi, alohida commit |
   | `ItemByKey("5")` ishlamaydi yoki boshqa obyekt qaytaradi | `vissim_set_attribute_bulk` da raqamli satr kalitni `int` ga aylantirish yoki rad etish |
   | Vissim bola jarayoni Job Object'dan chiqa olmay xato beradi | `BREAKAWAY_OK` bayrog'i — faqat loyiha egasining roziligi bilan (docs/com-names.md) |
   | `vissim_close` doim `terminate`/`kill` bilan tugaydi ("Save changes?" dialogi) | hujjatda qayd etish; CLAUDE.md'dagi "yopishdan oldin saqlang" qoidasi |
   | `SigState` pauzadan tashqarida yozilmaydi yoki `ContrByCOM` avtomatik yoqilmaydi | `vissim_set_signal_state` tavsifi va ogohlantirishini yangilash |

4. Commit (o'zingiz yoki Claude Code orqali): `git add docs scripts src tests` →
   `git commit -m "M6: real Vissim verification"`. `.claude/` papkasini commit qilmang.

## 6. Muammolar

| Belgi | Sabab va yechim |
|---|---|
| Probe chiqish kodi `3`, xabarda `license dialog` | Litsenziya default emas yoki COM moduli yo'q; siz ochgan Vissim seat'ni band qilgan bo'lishi mumkin. Barcha Vissim'ni yoping, 1-qadamni takrorlang |
| `vissim_connect` `CO_E_CLASSSTRING` | COM server ro'yxatdan o'tmagan: Vissim'ni administrator sifatida oching → Help → Register COM Server |
| Test oxirida "Vissim PID ... was still running" | Test o'z Vissim'ini (faqat `-Embedding` va PID tekshirilgandan keyin) o'ldirgan va bu xatoni qayd etgan — test hisobotiga yozing (Job Object yoki KILL_ON_EXIT ishlamagan) |
| `job_object: false` | Vissim administrator sifatida ishlayotgan bo'lishi mumkin — oddiy foydalanuvchi bilan ishga tushiring |
| Testlar `skipped` | `-m vissim --vissim-com-license` berilmagan; qo'lda testlar uchun `--vissim-manual -s` ham kerak |
