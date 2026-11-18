# vissim-mcp v1.0 — test hisoboti

PLAN.md 10.4 qabul mezoni: "10.3 dagi 1–6 qo'lda o'tkazilgan, natijalar `docs/test-report-v1.md` da". Bu hujjat har bir
10.3 bandi uchun bitta bo'lim va M1–M5 avtomatik testlari natijalarini o'z ichiga oladi. M6 ni o'tkazish tartibi:
docs/m6-runbook.md.

- M6 o'tkazilgan sana: **2026-11-18** (hisobot shabloni: 2026-10-27)
- Kompyuter: `DESKTOP-88D92K2`, Windows 11 Pro 10.0.26200 (rus lokali, ANSI kod sahifasi 1251)
- Vissim: **PTV Vissim Academic 2026 (SP 10)**, `C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe`,
  interfeys tili — ruscha
- Python 3.12.10 64-bit (`D:\PTV-Vissim\.venv`), mcp 1.30.0, pywin32 312; server `1.1.0rc1`
- Litsenziya: Academic — CodeMeter ro'yxatida `PTV Base License (UFC 6000459)` (yonida `PTV Vissim (Demo)`); COM
  orqali ishlaydi. Sinov paytida loyiha egasining 2–3 ta interaktiv Vissim oynasi ham ochiq edi — ularga tegilmadi.
- Model: `Examples Training\COM\Basic Commands\COM Basic Commands.inpx` ning vaqtinchalik nusxasi

## Umumiy holat

| 10.3 bandi | Holat | Sana | Vosita |
|---|---|---|---|
| 1. Smoke | PASSED | 2026-11-18 | `tests/real/test_real_smoke.py` |
| 2. Nomlarni tasdiqlash | PASSED — 32 ta nom `verified`, 0 `rejected` | 2026-11-18 | `scripts/probe_com_names.py --update`, `tests/real/test_real_com_names.py` |
| 3. Break semantikasi | PASSED (3 ta test; bitta kod xatosi topildi va tuzatildi) | 2026-11-18 | `tests/real/test_real_break_semantics.py` |
| 4. Stress | PASSED (2 ta test) | 2026-11-18 | `tests/real/test_real_stress.py` |
| 5. Nosozlik | PASSED avtomatik qismi (3 ta test); **qo'lda C1, C2, C3 o'tkazilmagan** | 2026-11-18 | `tests/real/test_real_failures.py` + qo'lda qadamlar |
| 6. Kodirovka | PASSED | 2026-11-18 | `tests/real/test_real_encoding.py` |

Yakuniy ishga tushirish (barcha tuzatishlardan keyin):

```text
> python -X utf8 -m pytest -p no:cacheprovider tests\real -m vissim --vissim-com-license -rA --vissim-results docs\m6-real-results.jsonl
13 passed, 3 skipped in 681.16s (0:11:21)          (server 1.1.0rc1, 24 ta tool)
SKIPPED: test_modal_dialog_gives_timeout_hung_and_restart_recovers (qo'lda qadam, --vissim-manual -s kerak)
SKIPPED: test_large_model_workflow[mainz-motorway]  - Vissim rad etdi: tarmoq hajmi 10000 m dan katta (litsenziya)
SKIPPED: test_large_model_workflow[boise-signals]   - Vissim rad etdi: 20 tadan ko'p svetofor kontrolleri (litsenziya)
> python -X utf8 scripts\probe_com_names.py --i-have-a-com-license --update
chiqish kodi 0; 32 ta nom verified
> python -X utf8 -m pytest -p no:cacheprovider -q        (soxta COM qatlami)
1016 passed, 1 skipped, 16 deselected; ruff toza
```

Shu yakuniy ishga tushirishda smoke testining `vissim_connect` i birinchi urinishda litsenziya dialogiga uchradi va
ikkinchi urinishda ulandi (`attempts: 2`, 40,6 s) — 4-topilmadagi qayta urinish amalda ishladi.

Birinchi o'tkazish (server 1.0.0rc2, 22 ta tool): 11 passed, 1 skipped (570,82 s).

Kuzatuvlar (har bir test yozgan qiymatlar): `docs/m6-real-results.jsonl`.

### M6 da topilgan va tuzatilgan kamchiliklar

| # | Topilma (haqiqiy Vissim'da) | Tuzatish |
|---|---|---|
| 1 | `vissim_set_signal_state`: Vissim `ContrByCOM` ni `1/0` qaytaradi, server `True` kutgan — har safar yolg'on ogohlantirish ("ContrByCOM read back as 1 ... the signal controller may override the state"), natijada `ContrByCOM: 1` | `tools/signals.py`: qiymat `true/false` ga aylantiriladi, ogohlantirish faqat haqiqiy farqda |
| 2 | Simulyatsiyadan tashqarida `SigState`/`ContrByCOM` bo'sh o'qiladi va yozib bo'lmaydi; server buni umumiy "signal nomini tekshiring" maslahati bilan qaytargan | Aniq maslahat: `pause_at` bilan pauza → holatni yozish → `vissim_resume_simulation`; tool tavsifi tuzatildi |
| 3 | `vissim_describe(include_attributes=true)`: `code` ustuni interfeys tilida (`№`, `Имя`) — bunday nom bilan `AttValue` xato beradi, ya'ni ro'yxat amalda yaroqsiz edi | Har bir atribut uchun `id` (`IAttribute.ID`: `NO`, `NAME`, ...) qaytariladi; tavsifda `id` ishlatilishi aytilgan |
| 4 | COM orqali ochilgan Vissim to'g'ri litsenziya bilan ham vaqti-vaqti bilan (taxminan har 8 ishga tushishdan birida) litsenziya dialogini ko'rsatdi yoki `CO_E_SERVER_EXEC_FAILURE` berdi; `vissim_connect`/`vissim_restart` darhol xato qaytargan | `service.py`: bunday xatodan keyin 5 s kutib yana 2 martagacha urinish; natijada `attempts` va ogohlantirish; `vissim_close`/`vissim_restart` kutishni bekor qila oladi. Sababi aniqlanmagan (o'sha paytda boshqa Vissim oynalari ochiq edi); 14 ta ketma-ket "yop → darhol och" tajribasida takrorlanmadi |
| 5 | `vissim_save_network(save_as="work\variant1.inpx")` va `vissim_export_csv` papka bo'lmasa `FILE_NOT_FOUND` — README'dagi odatiy birinchi qadam ishlamagan, Claude'da papka yaratadigan tool yo'q | Ruxsat etilgan ildiz ichida yo'q papka yaratiladi (`created_folder`, auditga yoziladi); faqat-o'qish va tashqi yo'llar avvalgidek rad etiladi |
| 6 | `vissim_start_simulation(runs>1)`: `KeepPrevResults` allaqachon `KEEPALL` bo'lsa ham "ensure evaluation keeps previous runs" ogohlantirishi chiqqan | Qiymat o'qiladi: `KEEPALL`/`3` da ogohlantirish yo'q; boshqa qiymatda aniq ko'rsatma bilan ogohlantirish |
| 7 | `scripts/doctor.ps1`: to'liq litsenziya CodeMeter'da "PTV Base License" deb nomlangani uchun "only PTV Vissim (Demo) - no COM module" FAIL chiqqan | PTV litsenziyalari ham hisobga olinadi; Demo/Student bo'lmagani bor bo'lsa PASS |
| 8 | Server log'i: Claude Code yuboradigan `server/discover` so'rovi uchun mcp SDK har sessiyada ~100 qator pydantic xatosini yozgan (`vissim_tail_log` foydasiz bo'lib qolgan) | `logging_setup.py`: bunday yozuv bitta INFO qatorga qisqartiriladi |
| 9 | `tests/real`: testlar `warnings` kaliti har doim bor deb kutgan (bo'sh bo'lsa server uni qo'shmaydi) — 2 ta test `KeyError` bilan yiqilgan | `.get("warnings", [])` |
| 10 | `probe_com_names.py`: type info'dan `SetAttValue` ni kutgan — u type library'da yo'q (pywin32 ning `AttValue` ga yozish nomi) | Kutilma: `AttValue` `property_get` + `property_put`; `IAttribute.ID` ham tekshiriladi |

Soxta COM qatlami (`tests/fake_vissim.py`) haqiqiy xatti-harakatga yaqinlashtirildi: `IAttribute.ID` qo'shildi,
`Code`/`Name` "tarjima qilingan" (identifikator emas).

Kodni o'zgartirishni talab qilmagan natijalar: `VISSIM_BREAK_DISABLE_VALUE=0` va `VISSIM_SEED_ATTR=RandSeed` to'g'ri;
`IsSimBreakAtActive` ni yozish kerak emas (`SimBreakAt` yozilganda o'zi yoqiladi); `ItemByKey("1")` ishlaydi; Job Object
flag'lari yetarli; `Exit()` 3,7–6 s da toza yopadi.

### M6 dan keyingi kengaytma (2026-11-18): loyihani to'liq MCP orqali bajarish

Birinchi M6 o'tkazilgandan keyin ikki kamchilik qolgan edi: tarmoq qurish imkoni yo'qligi va faqat 34 havolali
modelda sinalgani. Ikkalasi ham bartaraf etildi va haqiqiy Vissim'da tekshirildi.

| # | Nima qilindi | Haqiqiy Vissim'dagi natija |
|---|---|---|
| 11 | Yangi tool `vissim_call_method`: tarmoqni tahrirlovchi COM metodlari (`Add…`, `Remove…`, `Duplicate…` ..., ildizda `New`), obyekt argumentlari `{"ref": yo'l}`, ruxsat ro'yxati, audit | `test_real_build_network.py`: `New` → 3 havola → 2 ulagich → oqim 1200 avt/soat → marshrutlar 3:1 → o'lchovlar → saqlash → 600 s simulyatsiya: ma'lumot yig'ish nuqtasidan 69, sayohat vaqti o'lchovidan 133 avtomobil o'tdi (o'rtacha 25,1 s) |
| 12 | Yangi tool `vissim_set_signal_program`: qat'iy vaqtli kontroller dasturini (Vissig XML) tuzib `IntSupplyData` ga yozadi | Sikl 60 s, yashil 10–30 s: kuzatilgan holatlar 5 s `RED`, 9 `REDAMBER`, 20 `GREEN`, 31 `AMBER`, 34 `RED`, 80 `GREEN`, 100 `RED` — dasturga aynan mos |
| 13 | `vissim_describe` metodlarning parametr nomlarini qaytaradi | `AddLink` → `Key, WktLinestring, LaneWidths` |
| 14 | Fayli yo'q (yangi) tarmoqda `vissim_start_simulation` rad etiladi | Avval Vissim "Saqlash" dialogini ochib, COM 120 s osilib qolardi; endi darhol `INVALID_ARGUMENT` va "avval saqlang" maslahati |
| 15 | Vissim'ning o'z xabarlari xatoga qo'shiladi (`details.vissim_messages`): `.err` fayli va `vissim_msgs.txt` | Dasturi yo'q kontroller: "The signal groups defined in Vissim are different from those in the external data files"; yuklanmagan tarmoq: litsenziya cheklovi matni |
| 16 | Run boshlanmay qolgandan keyin keyingi runlar ham yiqilishi (Vissim xususiyati) aniqlandi | Xato maslahati: sababni tuzatish → saqlash → `vissim_load_network`; shu yo'l bilan run qayta ishladi |
| 17 | `TIMEOUT_HUNG` xabarida Vissim'ning ochiq oynalari ko'rsatiladi | Modal dialog holati ("Файл сохранить как") haqiqiy Vissim'da kuzatildi — C2 qadamining mohiyati (dialog → `TIMEOUT_HUNG`) shu bilan tasdiqlandi |
| 18 | Fayl/yo'l atributlariga ruxsat etilgan papka ichidagi yo'lni yozish mumkin; matn qiymati chegarasi 10 000 → 500 000 belgi | (soxta qatlamda tekshirilgan) |
| 19 | Katta model testi `test_real_large_model.py` | **Karlsruhe 3D** (8,4 MB, 2579 havola, 19 kontroller, 98 svetofor boshi): yuklash 13,8 s; barcha havolalarni 6 sahifada o'qish 0,14 s; 900 s simulyatsiya 20 s; 600-soniyada 217 avtomobil ro'yxati va CSV eksport; `vissim_health` < 1 ms; nusxasi saqlandi. **Mainz** va **Boise** — Vissim litsenziya cheklovi bilan rad etdi (pastga qarang) |

Academic litsenziya cheklovlari (Vissim'ning o'z xabarlari): tarmoq hajmi **10 000 m** dan, svetofor kontrollerlari
soni **20** tadan oshmasligi kerak. `Mainz - Static Routes` birinchisiga, `Signalization Boise` ikkinchisiga
tushdi. Bu server kamchiligi emas, lekin kattaroq loyihalar uchun to'liq litsenziya kerakligini bildiradi.

### Barcha tahrirlovchi metodlarni sinash (2026-11-18)

`vissim_describe` orqali haqiqiy Vissim'dan `Net` ostidagi **80 kolleksiyaning 250 dan ortiq** `Add…/Duplicate…/
Remove…/Generate…` metodi ro'yxati olindi va har biri `vissim_call_method` orqali chaqirildi; natijada doimiy test
`tests/real/test_real_methods.py` yozildi (2 ta test, 97 s: 80 ga yaqin yaratuvchi chaqiruv, ichki kolleksiyalar,
nusxalash/o'chirish, rad etiladigan holatlar, pauzadagi simulyatsiyaga avtomobil qo'shish).

| # | Topilma (haqiqiy Vissim'da) | Tuzatish |
|---|---|---|
| 20 | **18 ta metod jimgina hech narsa qilmagan**: massiv argumenti ichma-ich berilganda (`AddSpeedDistribution(0, [[40, 0], [60, 1]])`, `AddPriorityRule`, `AddConnectorEx`, funksiyalar) Vissim xato qaytarmaydi, obyekt ham yaratmaydi — server esa `ok: true` degan | `Add…/Duplicate…` obyekt qaytarmasa, kolleksiya chaqiruvdan oldin/keyin solishtiriladi: o'zgarmagan bo'lsa `INVALID_ARGUMENT` ("created nothing") va to'g'ri (tekis) formatlar maslahati; obyekt yaratilgan-u qaytarilmagan bo'lsa — kaliti topib beriladi |
| 21 | Enum parametrga nom berilsa (`"NORMAL"`, `"Integer"`) pywin32 `ValueError` beradi — server buni `INTERNAL` (ichki xato) deb qaytargan | `INVALID_ARGUMENT` va "enum raqam bilan beriladi" maslahati |
| 22 | `AddScript` va fayl yo'li oladigan metodlar (`AddBackgroundImage`, `AddStatic3DModel`, `AddModel2D3D`) ruxsat ro'yxatidan o'tib ketardi — fayl siyosatini va `vissim_exec_python` taqiqini chetlab o'tish yo'li | Skript metodlari taqiqlandi; yo'lga o'xshash har qanday satr argumenti ruxsat etilgan papkalar ichida bo'lishi shart |
| 23 | `vissim_set_attributes` fayl atributlari uchun yumshatilgan qoida `ScriptFile`, `ProgFile`, `…DLLFile` kabi **kod yuklaydigan** atributlarga ham tegishli bo'lib qolgan edi | Yumshatish faqat ma'lumot fayllariga (`SupplyFile1–3`, `PathFilename`, `ModelFilename`) qoldirildi |
| 24 | Ichki kolleksiya metodlari (`route.UpdateLinkSequence`, `…DatPts.ReplaceAll`) ruxsat etilmagan edi | `Update…` va `Replace…` prefikslari qo'shildi |
| 25 | Job pauzada turganda har qanday metod rad etilardi — simulyatsiya paytida avtomobil qo'shib bo'lmasdi | Faqat ishlayotgan job rad etadi; pauzada chaqiruv Vissim'ga yuboriladi (tarmoq tahririni Vissim o'zi rad etadi), `New` — har qanday faol job'da rad etiladi |
| 26 | Polosa (`AddLane`) uchun javobda kalit yo'q edi | Kalit sifatida `Index` qaytadi |

Sinalmay qolganlari docs/com-names.md jadvalining oxirgi qatorlarida: argument formati aniqlanmagan bir necha
funksiya (`AddBoardDelFunc`, `AddCurvSpeedFunc`, `AddLatDriftSpeedFunc`), dinamik taqsimlash yo'llari (`Net.Paths`),
simulyatsiya paytida piyoda qo'shish, va haqiqiy rasm/3D fayl bilan `AddBackgroundImage`/`AddStatic3DModel`.
Piyoda va jamoat transporti obyektlari **yaratildi**, lekin ular bilan simulyatsiya o'tkazilmagan.

### Ochiq qolganlar

- **Qo'lda qadamlar C1, C2, C3** (docs/m6-runbook.md, 4-bo'lim) o'tkazilmagan: ular loyiha egasining harakatini talab
  qiladi (Task Manager, Vissim oynasidagi "Save changes?" dialogi, Claude Code'ni yopish). C1 va C3 ning avtomatik
  ekvivalentlari o'tdi; C2 (`TIMEOUT_HUNG` modal dialogda) haqiqiy Vissim'da **tekshirilmagan**.
- Litsenziya dialogining sababi aniqlanmagan (4-topilma) — faqat qayta urinish bilan yumshatilgan.
- Probe'dagi `stale_break` tajribasi natijani `paused_at_0.0` deb yozadi; aslida run oxirigacha borgan (`SimSec=0`,
  `IsRunning=0`). Bu faqat probe hisobotidagi yorliq, serverga ta'sir qilmaydi.
- Bitta Vissim versiyasi/tili bilan sinalgan. Modellar: `COM Basic Commands` (34 havola), `Karlsruhe 3D` (2579
  havola) va noldan qurilgan chorraha. 10 000 m dan katta yoki 20 tadan ko'p kontrollerli tarmoqlar litsenziya
  sababli sinalmadi; soatlab davom etadigan real yuklama ham sinalmagan (Karlsruhe'da 900 s — 20 s).
- `vissim_call_method`: 80 kolleksiyaning metodlari sinalgan (yuqoridagi bo'lim); bir necha metod formati
  aniqlanmagan, piyoda va jamoat transporti obyektlari bilan simulyatsiya o'tkazilmagan.
- Svetofor: faqat qat'iy vaqtli dastur; to'qnashuvchi yashillar va oraliq vaqtlar tekshirilmaydi.

---

## 1. Smoke (PLAN 10.3, 1-band)

- **Holat:** PASSED — 2026-11-18, PTV Vissim Academic 2026 (SP 10)
- **Test:** `tests/real/test_real_smoke.py::test_smoke_connect_load_simulate_close`
- **Natija:** connect 12,3 s (`job_object: true`); yuklash 5,95 s, `Links=34`, layout yuklandi; 60 s simulyatsiya
  `finished` (0,47 s devor vaqti); `vissim_close` → `method: exit`; Vissim jarayoni qolmadi.
- **Topilmalar:** test `connected["warnings"]` kalitini kutgan (9-topilma) — tuzatildi.

## 2. Nomlarni tasdiqlash (PLAN 10.3, 2-band)

- **Holat:** PASSED — 2026-11-18. 32 ta nom `verified`, 0 `rejected`, 0 tekshirilmagan (docs/com-names.md).
- **Vosita:** `scripts/probe_com_names.py --i-have-a-com-license --update`; `tests/real/test_real_com_names.py`.
- **Natija:** xom jadval va JSON — shu hujjat oxirida ("Xom natijalar"). Semantik tajribalar xulosasi —
  docs/com-names.md, "M6 da aniqlangan xatti-harakatlar".
- **Topilmalar:** 3- va 10-topilmalar (`IAttribute.ID`; `SetAttValue` type library'da yo'q).

## 3. Break semantikasi (PLAN 10.3, 3-band)

- **Holat:** PASSED (3 ta test) — 2026-11-18
- **Natija:** `SimBreakAt=120` → `RunContinuous()` aynan 120,0 s da to'xtadi (`IsRunning=1`; `IsSimBreakAtActive`
  o'zi 0 → 1 bo'ldi). `SimBreakAt=0` bilan run oxirigacha bordi; run'dan keyin `SimSec=0`, `IsRunning=0`.
  O'tib ketgan break (`SimSec=120` da `SimBreakAt=60`) qolgan holatdan boshlangan job'lar (bitta bo'lak va bo'laklab)
  `finished` — ogohlantirish: "a simulation run may have been in progress (SimSec 120 s); it was stopped first".
  `pause_at=[200]` → 200 s da `paused` → `SigState="GREEN"` va `SigState=1` (→ `RED`) yozildi, `ContrByCOM=true` →
  `release_to_controller` → `ContrByCOM=false` → resume → `finished`.
- **Topilmalar:** 1-topilma (`ContrByCOM` `1/0`) aynan shu testda chiqdi va tuzatildi. `VISSIM_BREAK_DISABLE_VALUE`
  va `jobs.py` ga o'zgartirish kerak bo'lmadi.

## 4. Stress (PLAN 10.3, 4-band)

- **Holat:** PASSED (2 ta test) — 2026-11-18
- **Natija:** 5 run × 3600 s — `finished`, jami 35,1 s devor vaqti, har run 6,9–7,1 s; `percent` monoton;
  `vissim_health` eng ko'pi 0,001 s; `SimulationRuns`: urug'lar 42, 43, 44, 45, 46, har birida `SimEnd=3600`.
  `vissim_stop_simulation` 1,0 s da ta'sir qildi (`stopped`, bo'lak chegarasida).
- **Topilmalar:** 6-topilma (ortiqcha `KeepPrevResults` ogohlantirishi). Model kichik bo'lgani uchun simulyatsiya juda
  tez — soatlab davom etadigan haqiqiy yuklama bilan sinalmagan.

## 5. Nosozlik (PLAN 10.3, 5-band)

- **Holat:** avtomatik qismi PASSED (3 ta test) — 2026-11-18; **qo'lda C1, C2, C3 o'tkazilmagan**
- **Natija:** o'z Vissim'i o'ldirilgandan keyin `vissim_info` → `COM_ERROR` (HRESULT `0x800706BA`, "Сервер RPC
  недоступен", `disconnected: true`), maslahat: "Call vissim_restart"; `vissim_health`: `connected=false`,
  `vissim_alive=false`; `vissim_restart` yangi Vissim ochdi va tarmoq qayta yuklandi. Server jarayoni o'ldirilganda
  Vissim 0,62 s da yo'qoldi (Job Object). Stdin yopilganda server 0,119 s da chiqdi (chegarasi 0,300 s), Vissim yopildi.
- **O'tkazilmagan:** `test_modal_dialog_gives_timeout_hung_and_restart_recovers` (C2) — SKIPPED; C1 (Task Manager) va
  C3 (haqiqiy Claude Code'ni yopish) ham qo'lda bajarilmagan.
- **Topilmalar:** 4-topilma (litsenziya dialogi) va 9-topilma.

## 6. Kodirovka (PLAN 10.3, 6-band)

- **Holat:** PASSED — 2026-11-18
- **Natija:** `Samarqand_ko'cha_Самарқанд\tarmoq_ўзбек.inpx` yuklandi (ogohlantirish: "non-ASCII path; ..."),
  saqlandi va CSV eksport qilindi (34 qator). Vissim 2026 bunday yo'lni muammosiz o'qidi. Qo'shimcha: havola nomiga
  `Ko'cha Ўзбек 1` yozildi va qayta o'qildi; ruscha COM xato matnlari javobda to'g'ri (UTF-8) keladi.
- **Topilmalar:** yo'q.

---

## M1–M5 avtomatik testlari (haqiqiy Vissim'siz)

Barcha testlar soxta COM qatlami (`tests/fakes`, `tests/fake_vissim.py`, `FakeProcs`) bilan ishlaydi; hech biri
haqiqiy Vissim'ni ochmaydi va hech qanday `Vissim*.exe` jarayoniga tegmaydi (implementation contract, 0-bo'lim).

Buyruqlar (repozitoriya ildizida):

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -X utf8 -m ruff check src tests scripts
```

Natija (2026-11-18, M6 tuzatishlari va kengaytmadan keyin): **1022 passed, 1 skipped, 18 deselected** (~90 s);
ruff toza.
Skip: `tests/unit/test_policy.py::test_symlink_escape_is_denied` — bu kompyuterda symlink yaratish uchun huquq yo'q
(`WinError 1314`); junction va 8.3 nomlar bilan chetlab o'tish testlari o'tadi. 18 deselected — `tests/real`.
2026-10-27 dagi (M6 tayyorgarlik) natija: 908 passed; M7 dan keyin 970.

Bosqichlar tarixi (commit'lar bo'yicha):

| Bosqich | Commit | To'liq to'plam |
|---|---|---|
| M1 skelet | `ab30961` | yashil, ruff toza |
| M2 worker va hayot sikli | `b3c1b89`, `d62ab8f` | yashil, ruff toza |
| M3 fayllar va obyektlar | `f717060`, `2efd57b` | yashil, ruff toza |
| M4 simulyatsiya | `ca9d1b3`, `56bb355` | 864 test, yashil |
| M5 signallar va diagnostika | `18977a1` | 892 passed, 1 skipped |
| M6 tayyorgarlik | shu branch | 908 passed, 1 skipped, 12 deselected |

Modullar bo'yicha (2026-10-27 dagi soni):

| Bosqich | Test fayli | Testlar | Nimani tekshiradi |
|---|---|---|---|
| M1 | `unit/test_config.py` | 154 | PLAN 5 o'zgaruvchilari, standartlar, noto'g'ri qiymatlar |
| M1 | `unit/test_errors.py` | 65 | 16 xato kodi, HRESULT → kod/hint |
| M1 | `unit/test_output.py` | 33 | NaN/Inf, ichma-ich ro'yxat, qisqartirish, `MAX_RESULT_CHARS` |
| M1 | `unit/test_logging_setup.py` | 8 | aylanuvchi log, stderr |
| M1 | `unit/test_fake_modules.py`, `unit/test_fake_vissim.py` | 25 + 70 | soxta pywin32 va FakeVissim semantikasi |
| M1 | `protocol/test_stdio_protocol.py`, `protocol/test_stdio_guard.py`, `protocol/test_standin_protocol.py` | 15 + 4 + 4 | stdout faqat JSON-RPC (print, `os.write(1)`, native yozuv bilan ham), stdin EOF → ≤ 300 ms |
| M2 | `unit/test_backend.py`, `unit/test_worker.py`, `unit/test_service.py`, `unit/test_service_lifecycle.py` | 9 + 18 + 6 + 86 | STA worker, timeout → `TIMEOUT_HUNG`, restart, close, litsenziya dialogi, PID qoidasi |
| M2 | `unit/test_winproc.py` | 43 | PID farqi, `-Embedding` qoidasi, Job Object, PID noma'lumda kill yo'q |
| M2 | `unit/test_lifecycle_tools.py`, `protocol/test_lifecycle_protocol.py` | 13 + 5 | 5 ta hayot sikli tooli |
| M3 | `unit/test_policy.py`, `unit/test_resolver.py`, `unit/test_audit.py` | 79 + 50 + 9 | fail-closed yo'llar, readonly, ADS, yo'l atributlari; rezolver; JSONL audit |
| M3 | `unit/test_files_tools.py`, `unit/test_objects_tools.py`, `protocol/test_files_objects_protocol.py` | 19 + 33 + 2 | yuklash/saqlash, describe, list/get/set, bulk, `Samarqand_ko'cha` yo'li |
| M4 | `unit/test_jobs.py`, `unit/test_simulation_tools.py`, `unit/test_results_tools.py`, `protocol/test_simulation_protocol.py` | 67 + 18 + 10 + 1 | job holat mashinasi (eski break ham), wait ≤ `VISSIM_WAIT_MAX_S`, CSV eksport |
| M5 | `unit/test_signals_tools.py`, `unit/test_diagnostics_tools.py`, `protocol/test_signals_diagnostics_protocol.py` | 10 + 15 + 3 | signal faqat pauzada/job'siz, exec standart o'chiq, tail_log |
| M1–M5 | `unit/test_server.py`, `unit/test_tools_common.py` | 7 + 12 | 22 tool katalogi, javob formati |
| M6 tayyorgarlik | `unit/test_probe_com_names.py`, `unit/test_real_suite_wiring.py` | 12 + 4 | probe skripti FakeVissim bilan, `--help`/rad etish, hujjat yangilash; `tests/real` izolyatsiyasi |

---

## Xom natijalar

`scripts/probe_com_names.py --update` va `--vissim-results` bilan yig'ilgan natijalar shu bo'limdan keyin qo'shiladi.

## probe_com_names.py xom natijalari — 2026-11-18T15:59:47+05:00

Natija: 32 ok, 0 failed, 0 inconclusive, 0 skipped

```text
name                                                                    status        observed
----------------------------------------------------------------------  ------------  ----------------------------------------
`Vissim.LoadNet(path, additive)`                                        ok            loaded COM Basic Commands.inpx in 5.12 s; Links.Count=34
`Vissim.LoadLayout(path)`                                               ok            loaded COM Basic Commands.layx
`Vissim.SaveNetAs(path)`                                                ok            SaveNetAs -> file exists=True, size=227401
`Vissim.Exit()`                                                         ok            closed=True, method=exit, 3.7 s
`Vissim.SuspendUpdateGUI()` / `ResumeUpdateGUI(bool)`                   ok            SuspendUpdateGUI() and ResumeUpdateGUI(True) returned
`Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics`                 ok            Net=True, Simulation=True, Evaluation=True, Graphics=True
`Graphics.CurrentNetworkWindow`                                         ok            CurrentNetworkWindow=object
`Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()`             ok            RunContinuous/RunSingleStep/Stop returned; after one step SimSec=0.2
`<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)`          ok            Links.Count=34; ItemKeyExists(1)=True; ItemKeyExists(987654321)=False; ItemByKey(1).No=1
`Net.Links`, `.Nodes`, `.SignalControllers`, `.SignalHeads`, `.Vehi...  ok            Links=34, Nodes=0, SignalControllers=1, SignalHeads=5, VehicleInputs=3, VehicleRoutingDecisionsStatic=3, DataCollectionMeasurements=2, VehicleTravelTimeMeasurements=6, QueueCounters=1
`<collection>.GetMultipleAttributes(tuple)`                             ok            34 rows of (No, Name); first=(1, 'Old Link Name')
`<obj>.AttValue(name)` / `.SetAttValue(name, value)`                    ok            Link 1 Name='Old Link Name'; write-back read 'Old Link Name'
`<collection>.Attributes.GetAll()` → `IAttribute.Code/Name/ValueTyp...  ok            69 attributes; first: {'ID': 'NO', 'Code': '№', 'Name': 'Номер', 'ValueType': 1, 'Editable': True}
`_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)...  ok            Simulation: 7 members; AttValue ['property_get', 'property_put'] and RunContinuous present
`Net.SignalControllers[n].SGs[m]`                                       ok            Net.SignalControllers[1].SGs[1] -> No=1
`Net.SimulationRuns` (`GetMultipleAttributes(('No','RandSeed','SimE...  ok            3 runs; last=(3, 3, 0.2)
`SimPeriod`                                                             ok            SimPeriod=600 (write-back ok)
`SimRes`                                                                ok            SimRes=5 (write-back ok)
`SimSec`                                                                ok            SimSec=0.0
`SimBreakAt`                                                            ok            SimBreakAt=0.0 (write-back ok)
`RandSeed`                                                              ok            RandSeed=3 (write-back ok)
`UseMaxSimSpeed`                                                        ok            UseMaxSimSpeed=1 (write-back ok)
`NumCores`                                                              ok            NumCores=1; UseAllCores=1
`IsRunning`                                                             ok            IsRunning=0
`IsSimBreakAtActive`                                                    ok            IsSimBreakAtActive=0
`NumRuns`, `RandSeedIncr`                                               ok            NumRuns=1 (write-back ok); RandSeedIncr=1 (write-back ok)
`QuickMode`                                                             ok            QuickMode=0 (write-back ok)
`KeepPrevResults`                                                       ok            KeepPrevResults='KEEPALL' (write-back ok)
`SigState`                                                              ok            SigState=None
`ContrByCOM`                                                            ok            ContrByCOM=None
`VISSIM_SEED_ATTR`                                                      ok            RandSeed=3 (write-back read 3)
`VISSIM_BREAK_DISABLE_VALUE`                                            ok            SimBreakAt=0 then RunContinuous: ran_to_end=True, SimSec after the run=0.0
```

<details><summary>JSON</summary>

```json
{
 "started": "2026-11-18T15:59:47+05:00",
 "model": "C:\\Users\\UltraPC\\AppData\\Local\\Temp\\vissim-probe-k4ie70iy\\model\\COM Basic Commands.inpx",
 "layout": "C:\\Users\\UltraPC\\AppData\\Local\\Temp\\vissim-probe-k4ie70iy\\model\\COM Basic Commands.layx",
 "workdir": "C:\\Users\\UltraPC\\AppData\\Local\\Temp\\vissim-probe-k4ie70iy",
 "settings": {
  "period": 600,
  "break_at": 120,
  "break_disable_value": 0,
  "seed_attr": "RandSeed"
 },
 "connect": {
  "pid": 27024,
  "progid": "Vissim.Vissim",
  "job_object": true,
  "generation": 1,
  "elapsed_s": 15.4,
  "warnings": []
 },
 "process_after_connect": {
  "pid": 27024,
  "job_object": true,
  "children": [
   {
    "pid": 12024,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 3688,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 31284,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 12900,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 32964,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 6592,
    "name": "msedgewebview2.exe",
    "in_job": true
   },
   {
    "pid": 31104,
    "name": "msedgewebview2.exe",
    "in_job": true
   }
  ],
  "vissim_in_job": true
 },
 "keys": {
  "first_link_key": 1,
  "sc_key": 1,
  "sg_key": 1
 },
 "process_after_runs": {
  "pid": 27024,
  "job_object": true,
  "children": [],
  "vissim_in_job": true
 },
 "probes": [
  {
   "name": "`Vissim.LoadNet(path, additive)`",
   "status": "ok",
   "observed": "loaded COM Basic Commands.inpx in 5.12 s; Links.Count=34",
   "data": {}
  },
  {
   "name": "`Vissim.LoadLayout(path)`",
   "status": "ok",
   "observed": "loaded COM Basic Commands.layx",
   "data": {}
  },
  {
   "name": "`Vissim.SaveNetAs(path)`",
   "status": "ok",
   "observed": "SaveNetAs -> file exists=True, size=227401",
   "data": {
    "path": "C:\\Users\\UltraPC\\AppData\\Local\\Temp\\vissim-probe-k4ie70iy\\probe_saved.inpx"
   }
  },
  {
   "name": "`Vissim.Exit()`",
   "status": "ok",
   "observed": "closed=True, method=exit, 3.7 s",
   "data": {
    "closed": true,
    "method": "exit",
    "pid": 27024,
    "warnings": [],
    "elapsed_s": 3.7
   }
  },
  {
   "name": "`Vissim.SuspendUpdateGUI()` / `ResumeUpdateGUI(bool)`",
   "status": "ok",
   "observed": "SuspendUpdateGUI() and ResumeUpdateGUI(True) returned",
   "data": {}
  },
  {
   "name": "`Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics`",
   "status": "ok",
   "observed": "Net=True, Simulation=True, Evaluation=True, Graphics=True",
   "data": {
    "Net": true,
    "Simulation": true,
    "Evaluation": true,
    "Graphics": true
   }
  },
  {
   "name": "`Graphics.CurrentNetworkWindow`",
   "status": "ok",
   "observed": "CurrentNetworkWindow=object",
   "data": {}
  },
  {
   "name": "`Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()`",
   "status": "ok",
   "observed": "RunContinuous/RunSingleStep/Stop returned; after one step SimSec=0.2",
   "data": {}
  },
  {
   "name": "`<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)`",
   "status": "ok",
   "observed": "Links.Count=34; ItemKeyExists(1)=True; ItemKeyExists(987654321)=False; ItemByKey(1).No=1",
   "data": {}
  },
  {
   "name": "`Net.Links`, `.Nodes`, `.SignalControllers`, `.SignalHeads`, `.VehicleInputs`, `.VehicleRoutingDecisionsStatic`, `.DataCollectionMeasurements`, `.VehicleTravelTimeMeasurements`, `.QueueCounters`",
   "status": "ok",
   "observed": "Links=34, Nodes=0, SignalControllers=1, SignalHeads=5, VehicleInputs=3, VehicleRoutingDecisionsStatic=3, DataCollectionMeasurements=2, VehicleTravelTimeMeasurements=6, QueueCounters=1",
   "data": {
    "Links": 34,
    "Nodes": 0,
    "SignalControllers": 1,
    "SignalHeads": 5,
    "VehicleInputs": 3,
    "VehicleRoutingDecisionsStatic": 3,
    "DataCollectionMeasurements": 2,
    "VehicleTravelTimeMeasurements": 6,
    "QueueCounters": 1
   }
  },
  {
   "name": "`<collection>.GetMultipleAttributes(tuple)`",
   "status": "ok",
   "observed": "34 rows of (No, Name); first=(1, 'Old Link Name')",
   "data": {}
  },
  {
   "name": "`<obj>.AttValue(name)` / `.SetAttValue(name, value)`",
   "status": "ok",
   "observed": "Link 1 Name='Old Link Name'; write-back read 'Old Link Name'",
   "data": {}
  },
  {
   "name": "`<collection>.Attributes.GetAll()` → `IAttribute.Code/Name/ValueType/Editable`",
   "status": "ok",
   "observed": "69 attributes; first: {'ID': 'NO', 'Code': '№', 'Name': 'Номер', 'ValueType': 1, 'Editable': True}",
   "data": {
    "ID": "NO",
    "Code": "№",
    "Name": "Номер",
    "ValueType": 1,
    "Editable": true
   }
  },
  {
   "name": "`_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)` → `GetNames(memid)`",
   "status": "ok",
   "observed": "Simulation: 7 members; AttValue ['property_get', 'property_put'] and RunContinuous present",
   "data": {}
  },
  {
   "name": "`Net.SignalControllers[n].SGs[m]`",
   "status": "ok",
   "observed": "Net.SignalControllers[1].SGs[1] -> No=1",
   "data": {}
  },
  {
   "name": "`Net.SimulationRuns` (`GetMultipleAttributes(('No','RandSeed','SimEnd'))`)",
   "status": "ok",
   "observed": "3 runs; last=(3, 3, 0.2)",
   "data": {
    "rows": [
     [
      1,
      3,
      600.0
     ],
     [
      2,
      3,
      600.0
     ],
     [
      3,
      3,
      0.2
     ]
    ]
   }
  },
  {
   "name": "`SimPeriod`",
   "status": "ok",
   "observed": "SimPeriod=600 (write-back ok)",
   "data": {
    "path": "Simulation",
    "SimPeriod": 600,
    "SimPeriod.writeback": {
     "write": null,
     "read_again": 600
    }
   }
  },
  {
   "name": "`SimRes`",
   "status": "ok",
   "observed": "SimRes=5 (write-back ok)",
   "data": {
    "path": "Simulation",
    "SimRes": 5,
    "SimRes.writeback": {
     "write": null,
     "read_again": 5
    }
   }
  },
  {
   "name": "`SimSec`",
   "status": "ok",
   "observed": "SimSec=0.0",
   "data": {
    "path": "Simulation",
    "SimSec": 0.0
   }
  },
  {
   "name": "`SimBreakAt`",
   "status": "ok",
   "observed": "SimBreakAt=0.0 (write-back ok)",
   "data": {
    "path": "Simulation",
    "SimBreakAt": 0.0,
    "SimBreakAt.writeback": {
     "write": null,
     "read_again": 0.0
    }
   }
  },
  {
   "name": "`RandSeed`",
   "status": "ok",
   "observed": "RandSeed=3 (write-back ok)",
   "data": {
    "path": "Simulation",
    "RandSeed": 3,
    "RandSeed.writeback": {
     "write": null,
     "read_again": 3
    }
   }
  },
  {
   "name": "`UseMaxSimSpeed`",
   "status": "ok",
   "observed": "UseMaxSimSpeed=1 (write-back ok)",
   "data": {
    "path": "Simulation",
    "UseMaxSimSpeed": 1,
    "UseMaxSimSpeed.writeback": {
     "write": null,
     "read_again": 1
    }
   }
  },
  {
   "name": "`NumCores`",
   "status": "ok",
   "observed": "NumCores=1; UseAllCores=1",
   "data": {
    "path": "Simulation",
    "NumCores": 1,
    "UseAllCores": 1
   }
  },
  {
   "name": "`IsRunning`",
   "status": "ok",
   "observed": "IsRunning=0",
   "data": {
    "path": "Simulation",
    "IsRunning": 0
   }
  },
  {
   "name": "`IsSimBreakAtActive`",
   "status": "ok",
   "observed": "IsSimBreakAtActive=0",
   "data": {
    "path": "Simulation",
    "IsSimBreakAtActive": 0
   }
  },
  {
   "name": "`NumRuns`, `RandSeedIncr`",
   "status": "ok",
   "observed": "NumRuns=1 (write-back ok); RandSeedIncr=1 (write-back ok)",
   "data": {
    "path": "Simulation",
    "NumRuns": 1,
    "NumRuns.writeback": {
     "write": null,
     "read_again": 1
    },
    "RandSeedIncr": 1,
    "RandSeedIncr.writeback": {
     "write": null,
     "read_again": 1
    }
   }
  },
  {
   "name": "`QuickMode`",
   "status": "ok",
   "observed": "QuickMode=0 (write-back ok)",
   "data": {
    "path": "Graphics.CurrentNetworkWindow",
    "QuickMode": 0,
    "QuickMode.writeback": {
     "write": null,
     "read_again": 0
    }
   }
  },
  {
   "name": "`KeepPrevResults`",
   "status": "ok",
   "observed": "KeepPrevResults='KEEPALL' (write-back ok)",
   "data": {
    "path": "Evaluation",
    "KeepPrevResults": "KEEPALL",
    "KeepPrevResults.writeback": {
     "write": null,
     "read_again": "KEEPALL"
    }
   }
  },
  {
   "name": "`SigState`",
   "status": "ok",
   "observed": "SigState=None",
   "data": {
    "path": "Net.SignalControllers[1].SGs[1]",
    "SigState": null
   }
  },
  {
   "name": "`ContrByCOM`",
   "status": "ok",
   "observed": "ContrByCOM=None",
   "data": {
    "path": "Net.SignalControllers[1].SGs[1]",
    "ContrByCOM": null
   }
  },
  {
   "name": "`VISSIM_SEED_ATTR`",
   "status": "ok",
   "observed": "RandSeed=3 (write-back read 3)",
   "data": {}
  },
  {
   "name": "`VISSIM_BREAK_DISABLE_VALUE`",
   "status": "ok",
   "observed": "SimBreakAt=0 then RunContinuous: ran_to_end=True, SimSec after the run=0.0",
   "data": {}
  }
 ],
 "semantics": {
  "prepare": {
   "saved": {
    "SimPeriod": 600,
    "SimBreakAt": 0.0,
    "UseMaxSimSpeed": 1,
    "IsSimBreakAtActive": 0,
    "QuickMode": 0,
    "KeepPrevResults": "KEEPALL"
   },
   "set_KeepPrevResults_KeepAll": null,
   "KeepPrevResults": "KEEPALL",
   "idle": {
    "SimSec": 0.0,
    "IsRunning": 0,
    "SimulationRuns.Count": 0
   }
  },
  "signal_outside_run": {
   "path": "Net.SignalControllers[1].SGs[1]",
   "before": {
    "SigState": null,
    "ContrByCOM": null
   },
   "write_RED": {
    "error": "AttValue failed: Объект 1 - 1: West => East: Атрибут Вид сигнала не может быть изменен."
   },
   "after": {
    "SigState": null,
    "ContrByCOM": null
   },
   "release": {
    "error": "AttValue failed: Объект 1 - 1: West => East: Атрибут Управляемый COM не может быть изменен."
   },
   "after_release": {
    "ContrByCOM": null
   }
  },
  "break_at_n": {
   "N": 120,
   "time_step": 0.2,
   "IsSimBreakAtActive_before": 0,
   "IsSimBreakAtActive_after_set": 1,
   "run": {
    "wall_s": 0.34,
    "SimSec": 120.0,
    "IsRunning": 1,
    "SimulationRuns.Count": 1
   },
   "paused_at_N": true,
   "runs_before": 0
  },
  "signal_while_paused": {
   "path": "Net.SignalControllers[1].SGs[1]",
   "before": {
    "SigState": "REDAMBER",
    "ContrByCOM": 0
   },
   "write_GREEN": null,
   "after_GREEN": {
    "SigState": "GREEN",
    "ContrByCOM": 1
   },
   "write_int_1": null,
   "after_int_1": {
    "SigState": "RED",
    "ContrByCOM": 1
   },
   "release": null,
   "after_release": {
    "SigState": "RED",
    "ContrByCOM": 0
   }
  },
  "stale_break": {
   "stale_break": 60,
   "simsec_before": 120,
   "runs_before": 1,
   "IsSimBreakAtActive": 1,
   "IsSimBreakAtActive_forced_by_probe": false,
   "run": {
    "wall_s": 0.39,
    "SimSec": 0.0,
    "IsRunning": 0,
    "SimulationRuns.Count": 1
   },
   "outcome": "paused_at_0.0",
   "stop": {
    "SimSec": 0.0,
    "IsRunning": 0,
    "SimulationRuns.Count": 1
   }
  },
  "break_disable_runs_to_end": {
   "value": 0,
   "IsSimBreakAtActive": 1,
   "IsSimBreakAtActive_forced_by_probe": false,
   "runs_before": 1,
   "SimBreakAt_read_back": 0.0,
   "after": {
    "wall_s": 0.67,
    "SimSec": 0.0,
    "IsRunning": 0,
    "SimulationRuns.Count": 2
   },
   "last_run": {
    "No": 2,
    "RandSeed": 3,
    "SimEnd": 600.0
   },
   "ran_to_end": true
  },
  "single_step_and_stop": {
   "time_step": 0.2,
   "after_step": {
    "SimSec": 0.2,
    "IsRunning": 1,
    "SimulationRuns.Count": 3
   },
   "after_stop": {
    "SimSec": 0.0,
    "IsRunning": 0,
    "SimulationRuns.Count": 3
   },
   "runs_before": 2
  },
  "item_by_key_string": {
   "key": 1,
   "ItemKeyExists(str)": true,
   "ItemByKey(str).No": 1
  },
  "restore": {
   "SimPeriod": null,
   "SimBreakAt": null,
   "UseMaxSimSpeed": null,
   "IsSimBreakAtActive": null,
   "QuickMode": null,
   "KeepPrevResults": null,
   "ResumeUpdateGUI": null
  }
 },
 "info": {
  "load": {
   "LoadNet": "ok",
   "load_s": 5.12,
   "LoadLayout": "ok",
   "links": 34
  },
  "version": "PTV Vissim Academic 2026 (SP 10)",
  "final_save": {
   "path": "C:\\Users\\UltraPC\\AppData\\Local\\Temp\\vissim-probe-k4ie70iy\\probe_final.inpx",
   "saved": true,
   "result": null
  }
 },
 "close": {
  "closed": true,
  "method": "exit",
  "pid": 27024,
  "warnings": [],
  "elapsed_s": 3.7
 },
 "finished": "2026-11-18T16:00:15+05:00",
 "interactive_vissim_pids": [
  8,
  5088,
  10468
 ]
}
```

</details>
