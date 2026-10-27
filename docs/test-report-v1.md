# vissim-mcp v1.0 — test hisoboti

PLAN.md 10.4 qabul mezoni: "10.3 dagi 1–6 qo'lda o'tkazilgan, natijalar `docs/test-report-v1.md` da". Bu hujjat har bir
10.3 bandi uchun bitta bo'lim va M1–M5 avtomatik testlari natijalarini o'z ichiga oladi. M6 ni o'tkazish tartibi:
docs/m6-runbook.md.

- Hisobot shabloni tayyorlangan sana: **2026-10-27**
- Kompyuter: `DESKTOP-88D92K2`, Windows 11 Pro 10.0.26200 (rus lokali, ANSI kod sahifasi 1251)
- Vissim: PTV Vissim 2026 (2026.0.10.0), `C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe`
- Python 3.12.10 64-bit (`D:\PTV-Vissim\.venv`), mcp 1.30.0, pywin32 312
- Litsenziya: faqat **PTV Vissim (Demo)** — COM moduli yo'q (docs/preflight-report.md)

## Umumiy holat

| 10.3 bandi | Holat | Sana | Tayyor vosita |
|---|---|---|---|
| 1. Smoke | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `tests/real/test_real_smoke.py` |
| 2. Nomlarni tasdiqlash | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `scripts/probe_com_names.py`, `tests/real/test_real_com_names.py` |
| 3. Break semantikasi | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `tests/real/test_real_break_semantics.py` |
| 4. Stress | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `tests/real/test_real_stress.py` |
| 5. Nosozlik | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `tests/real/test_real_failures.py` + qo'lda qadamlar |
| 6. Kodirovka | BLOCKED: COM litsenziyasi yo'q (Demo) | 2026-10-27 | `tests/real/test_real_encoding.py` |

### Bloklanish dalili (M0, 2026-10-16)

Barcha 6 band uchun umumiy (docs/preflight-report.md):

- CodeMeter'dagi yagona Vissim litsenziyasi — `PTV Vissim (Demo)` (FC 6000459, PC 20000, 1 o'rin); Demo litsenziyada
  COM moduli bo'lmaydi.
- `scripts/com_smoke.py` (`Dispatch → Net.Links.Count → Exit`): COM orqali ochilgan nusxa
  (`Vissim260.exe -Embedding`) "Управление лицензиями – PTV Vissim" oynasini ko'rsatdi ("Лицензия не найдена");
  `Dispatch` 75–120 s dan keyin `CO_E_SERVER_EXEC_FAILURE` (0x80080005, −2146959355) bilan tugadi; Windows System
  log'ida DCOM 10010.
- Demo litsenziya bilan haqiqiy Vissim'da bitta ham COM chaqiruvi bajarilmagan, shuning uchun 10.3 bandlarining
  birortasini o'tkazib bo'lmaydi. Server ishga tushishi va litsenziya dialogini aniqlash mantig'i soxta (fake)
  qatlam bilan tekshirilgan (pastda).

### Haqiqiy test to'plamining holati (2026-10-27)

```text
> python -X utf8 -m pytest -p no:cacheprovider tests\real -m vissim --collect-only -q
12 tests collected
> python -X utf8 -m pytest -p no:cacheprovider tests\real -m vissim -q -rs
12 skipped  (sabab: "real Vissim test: needs --vissim-com-license ...")
> python -X utf8 scripts\probe_com_names.py
refused: ... confirm that with --i-have-a-com-license   (chiqish kodi 2)
```

Standart `pytest` ishga tushirishda `tests/real` testlari `deselected` (12 ta) bo'ladi va pytest jarayoniga haqiqiy
pywin32 yuklanmaydi (`tests/unit/test_real_suite_wiring.py` buni tekshiradi).

---

## 1. Smoke (PLAN 10.3, 1-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili (yuqorida).
- **Test:** `tests/real/test_real_smoke.py::test_smoke_connect_load_simulate_close` — connect → `COM Basic Commands`
  nusxasini yuklash → `Links.Count` → 60 s simulyatsiya → `vissim_close`; jarayon qolmasligi tekshiriladi.
- **Kutilgan natija:** `PASSED`; `job_object: true`; close usuli `exit`.
- **Natija:** _(M6 sessiyasida to'ldiriladi: sana, pytest xulosasi, `m6-real-results.jsonl` qatorlari)_
- **Topilmalar:** _(to'ldiriladi)_

## 2. Nomlarni tasdiqlash (PLAN 10.3, 2-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili. docs/com-names.md dagi barcha nomlar `documented` (type library, attribute.xlsx va
  rasmiy misol bo'yicha oflayn tekshirilgan), hech biri `verified` emas.
- **Vosita:** `scripts/probe_com_names.py --i-have-a-com-license [--update]` — docs/com-names.md ning har bir qatori
  uchun bitta tekshiruv (`PROBES` ro'yxati; jadval bilan mosligini `tests/unit/test_probe_com_names.py` tekshiradi)
  va semantik tajribalar (`SimBreakAt`, `IsSimBreakAtActive`, eski break, `SimSec` run'dan keyin, `SigState`/
  `ContrByCOM`, `ItemByKey` satr kalit, Job Object va bola jarayonlar, `Exit()`).
- **Test:** `tests/real/test_real_com_names.py::test_probe_com_names`.
- **Kutilgan natija:** server tayanadigan nomlar `ok`; qolgan nomlar `verified`/`rejected` bo'lib, docs/com-names.md
  da yakunlanadi.
- **Natija:** _(to'ldiriladi; `--update` xom natijalarni shu hujjat oxiriga qo'shadi)_
- **Topilmalar:** _(to'ldiriladi)_

## 3. Break semantikasi (PLAN 10.3, 3-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili. Hozirgi bilim faqat hujjatlardan: rasmiy misolda "`SimBreakAt` = 0 means no
  break", attribute.xlsx da `SimBreakAt` min 0, standart 0; `IsSimBreakAtActive` Vissim 2026 da yangi, ma'nosi
  noma'lum.
- **Testlar:** `tests/real/test_real_break_semantics.py` — `test_raw_break_at_and_break_disable_value` (xom COM:
  120 s da pauza, `SimBreakAt=0` bilan run oxirigacha, run'dan keyingi `SimSec`), `test_stale_break_does_not_break_a_job`
  (model run o'rtasida `SimSec`=120 da pauzada, `SimBreakAt=60` (o'tib ketgan) qolgan holat: xom `RunContinuous()` natijasi yoziladi, shu holatdan boshlangan job tugaydi), `test_pause_at_signal_state_and_resume` (`pause_at` → signal
  holati → resume).
- **Kutilgan natija:** 3 ta `PASSED`. Aks holda — `VISSIM_BREAK_DISABLE_VALUE` yoki `jobs.py` ga tuzatish (runbook 5-bo'lim).
- **Natija:** _(to'ldiriladi)_
- **Topilmalar:** _(to'ldiriladi)_

## 4. Stress (PLAN 10.3, 4-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili. Job holat mashinasi (bo'laklar, ko'p run, urug'lar, stop, `percent`) soxta Vissim
  bilan `tests/unit/test_jobs.py` (67 test) va `tests/protocol/test_simulation_protocol.py` da tekshirilgan.
- **Testlar:** `tests/real/test_real_stress.py` — `test_five_runs_of_an_hour` (5 × 3600 s, `percent` monoton,
  `vissim_health` < 1 s, `SimulationRuns` da 5 ta urug'), `test_stop_takes_effect_within_one_chunk`.
- **Kutilgan natija:** 2 ta `PASSED`; devor vaqti va har run vaqti (`run_wall_s`) yozib olinadi.
- **Natija:** _(to'ldiriladi)_
- **Topilmalar:** _(to'ldiriladi)_

## 5. Nosozlik (PLAN 10.3, 5-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili. M0 da kuzatilgan litsenziya dialogi (`Управление лицензиями – PTV Vissim`) COM
  chaqiruvini 75–120 s bloklashi server dizayniga kiritilgan: `vissim_connect` dialogni aniqlaydi va faqat o'zi
  ochgan jarayonni yopadi (soxta qatlam bilan tekshirilgan).
- **Testlar:** `tests/real/test_real_failures.py` — Vissim'ni o'ldirish → aniq xato → `vissim_restart` (avtomatik yoki
  `--vissim-manual` bilan Task Manager orqali); "Save changes?" dialogi → `TIMEOUT_HUNG` (faqat `--vissim-manual -s`);
  server o'ldirilsa Vissim yopiladi (Job Object); stdin yopilsa ≤ 300 ms. Claude Code'ni yopish — runbook C3.
- **Kutilgan natija:** 4 ta `PASSED`; C3 da Vissim jarayoni 10 s ichida yo'qoladi.
- **Natija:** _(to'ldiriladi)_
- **Topilmalar:** _(to'ldiriladi)_

## 6. Kodirovka (PLAN 10.3, 6-band)

- **Holat:** BLOCKED: COM litsenziyasi yo'q (Demo) — 2026-10-27
- **Dalil:** M0 bloklanish dalili. Server tomoni (UTF-8, `non-ASCII path` ogohlantirishi, `Samarqand_ko'cha` yo'li)
  soxta qatlam bilan `tests/protocol/test_files_objects_protocol.py` da tekshirilgan; Vissim'ning o'zi bunday yo'lni
  o'qiy olishi tekshirilmagan.
- **Test:** `tests/real/test_real_encoding.py::test_non_ascii_path_round_trip` — `Samarqand_ko'cha_Самарқанд` papkasida
  yuklash, saqlash va CSV eksport.
- **Kutilgan natija:** `PASSED`. Vissim o'qiy olmasa — README'da "loyihani ASCII yo'lga ko'chiring" tavsiyasi tasdiqlanadi.
- **Natija:** _(to'ldiriladi)_
- **Topilmalar:** _(to'ldiriladi)_

---

## M1–M5 avtomatik testlari (haqiqiy Vissim'siz)

Barcha testlar soxta COM qatlami (`tests/fakes`, `tests/fake_vissim.py`, `FakeProcs`) bilan ishlaydi; hech biri
haqiqiy Vissim'ni ochmaydi va hech qanday `Vissim*.exe` jarayoniga tegmaydi (implementation contract, 0-bo'lim).

Buyruqlar (repozitoriya ildizida):

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -X utf8 -m ruff check src tests scripts
.\.venv\Scripts\python.exe -X utf8 -m ruff format --check src tests scripts
```

Natija (2026-10-27, `m6-real-prep` branch): **908 passed, 1 skipped, 12 deselected** (~70 s); ruff toza.
Skip: `tests/unit/test_policy.py::test_symlink_escape_is_denied` — bu kompyuterda symlink yaratish uchun huquq yo'q
(`WinError 1314`); junction va 8.3 nomlar bilan chetlab o'tish testlari o'tadi. 12 deselected — `tests/real`.

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
