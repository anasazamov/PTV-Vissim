# vissim-mcp

PTV Vissim'ni COM interfeysi orqali boshqarish uchun **stdio MCP server** (Python). Claude Code (Windows'dagi
Desktop ilovasi yoki CLI) shu server orqali Vissim'ni ishga tushiradi, tarmoq yuklaydi, atributlarni o'qiydi/yozadi
va simulyatsiyani fon job sifatida yurgizadi. Loyihaning yagona manbai — [`PLAN.md`](PLAN.md).

## Holat

| Bosqich | Holat |
|---|---|
| M0 — Pre-flight | bajarildi ([`docs/preflight-report.md`](docs/preflight-report.md), [`docs/com-names.md`](docs/com-names.md)) |
| **M1 — Skelet** | **bajarildi**: konfiguratsiya, xato kodlari, log, stdout himoyasi, chiqish cheklovlari, faqat `vissim_health` tooli bor server, soxta (fake) COM qatlami, testlar, CI |
| M2–M7 | hali bajarilmagan (worker, fayllar, simulyatsiya, signallar, haqiqiy Vissim'da tekshirish, reliz) |

> Muhim: kompyuterdagi yagona Vissim litsenziyasi — Demo (COM moduli yo'q), shuning uchun haqiqiy Vissim bilan
> ishlash M6 gacha mumkin emas. Barcha testlar soxta COM qatlami bilan ishlaydi va haqiqiy Vissim'ga tegmaydi.

## Dasturchi muhiti

Talablar: Windows 10/11 (yoki testlar uchun Linux), Python 3.11+ (64-bit). Konsol kodirovkasi cp1251 bo'lgani uchun
Python har doim `-X utf8` bilan ishga tushiriladi.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`pywin32` faqat Windows'da o'rnatiladi (`sys_platform == 'win32'`). Server ishga tushganda pywin32'ning COM
qismlari (`pythoncom`, `win32com`) import qilinmaydi — COM faqat `vissim_connect` (M2) chaqirilganda ishlatiladi.

## Testlar

```powershell
.venv\Scripts\python.exe -X utf8 -m pytest
.venv\Scripts\python.exe -X utf8 -m ruff check src tests scripts
```

- `tests/unit` — config, errors, output, log, tool o'rami, server qismlari, service hamda soxta Vissim'ning o'zi
  (`RunContinuous`/`SimBreakAt` semantikasi, nosozlik in'ektsiyasi).
- `tests/protocol` — serverni `python -X utf8 -m vissim_mcp` subprocess sifatida ishga tushiradi
  (`PYTHONPATH=tests/fakes`, `PYTHONUTF8=1`, alohida log/audit kataloglari) va `initialize` →
  `notifications/initialized` → `tools/list` → `tools/call vissim_health` ketma-ketligini o'tkazadi. stdout'dagi
  **har bir qator** JSON-RPC ekani tekshiriladi — shu jumladan ataylab `print()`, `sys.stdout`/`sys.__stdout__`,
  `os.write(1, ...)` va Windows'da `WriteFile(GetStdHandle(STD_OUTPUT_HANDLE))` orqali "shovqin" qilingan holatda
  (`tests/protocol/noisy_launcher.py`; production kodida test uchun maxsus ilgak yo'q).
- stdin yopilgandan keyin jarayon **300 ms** ichida chiqishi o'lchanadi (o'lchov `stdin.close()` dan oldin
  boshlanadi; CI uchun qo'shimcha "zaxira" berilmagan). Mahalliy o'lchov (30 marta): 6–7 ms.
- `tests/real` (M6) — `@pytest.mark.vissim`, standart holatda o'tkazib yuboriladi (`-m "not vissim"`).

Soxta COM qatlami:

- `tests/fake_vissim.py` — `FakeVissim`: Net kolleksiyalari, atributlar (ba'zilari read-only), `Simulation`
  (`RunContinuous` `SimBreakAt > SimSec` bo'lsa o'sha joyda to'xtaydi, aks holda run oxirigacha), fayllar, type info,
  nosozliklar (`hang_next_call`, `raise_com_error_next`, `print_to_stdout_on_call`, `stale_break_at`,
  `ignore_break`, `crash()` ...). Har bir COM obyekt o'zini yaratgan oqimga bog'langan (boshqa oqimdan chaqirilsa
  `RPC_E_WRONG_THREAD`) — xuddi STA'dagidek.
- `tests/fakes/` — soxta `pythoncom`, `pywintypes`, `win32com.client.dynamic` (`Dispatch()` → `FakeVissim`,
  `FAKE_VISSIM_*` muhit o'zgaruvchilari bilan sozlanadi). `tests/conftest.py` ularni pywin32'dan oldin `sys.path`
  ga qo'yadi va `VISSIM_MCP_FORBID_REAL=1` o'rnatadi.
- `tests/fake_backend.py` — `FakeBackend` (in-process unit testlar uchun).

## Serverni qo'lda tekshirish

```powershell
.venv\Scripts\python.exe -X utf8 -m vissim_mcp --version   # versiya stdout'ga
.venv\Scripts\python.exe -X utf8 -m vissim_mcp --check     # konfiguratsiya JSON ko'rinishida stderr'ga
```

Chiqish kodlari: `0` — normal (stdin yopildi), `1` — kutilmagan xato, `2` — konfiguratsiya yoki buyruq qatori
xatosi, `130` — Ctrl+C. Konfiguratsiya xatosi stderr'ga o'zgaruvchi nomi bilan yoziladi, masalan:
`vissim-mcp: configuration error: VISSIM_CALL_TIMEOUT_S: must be greater than 0, got '-5'`.

## Konfiguratsiya (PLAN.md 5-bo'lim)

Barcha `VISSIM_*` o'zgaruvchilar ishga tushishda o'qiladi va tekshiriladi; joriy qiymatlarni `vissim_health`
(`settings`) va `--check` ko'rsatadi. Qoidalar:

- Bo'sh qiymat = standart qiymat. Istisno: `VISSIM_EXE_PATTERN` bo'sh bo'lsa — xato (PLAN 10.1).
- Mantiqiy qiymatlar: `1/0`, `true/false`, `yes/no`, `on/off`.
- Timeout'lar soniyada, `> 0` va `≤ 86400`; `VISSIM_WAIT_MAX_S` < 120 bo'lishi shart.
- `VISSIM_CHUNK_SIM_S` — butun son, 1…2 678 400; `VISSIM_MAX_ROWS` ≥ 1; `VISSIM_MAX_RESULT_CHARS` ≥ 1000
  (50 000 dan katta bo'lsa ogohlantirish: Claude Code bunday natijani faylga saqlaydi).
- Kataloglar ro'yxati `;` bilan ajratiladi, qo'shtirnoqlar va bo'sh elementlar olib tashlanadi, `~` ochiladi.
  `VISSIM_ALLOWED_DIRS` bo'sh bo'lsa — `CLAUDE_PROJECT_DIR`; ikkalasi ham bo'lmasa barcha fayl operatsiyalari rad
  etiladi (fail-closed) va `vissim_health` ogohlantiradi.
- `VISSIM_EXE_PATTERN` registrga befarq kompilyatsiya qilinadi va jarayon nomiga `fullmatch` bilan qo'llanadi;
  `explorer.exe`, `python.exe`, `svchost.exe` kabi tizim jarayonlariga mos keladigan naqsh rad etiladi.
- `VISSIM_PROGID` — ProgID (≤ 39 belgi) yoki `{CLSID}`; `VISSIM_SEED_ATTR` — atribut nomi;
  `VISSIM_BREAK_DISABLE_VALUE` — `≥ 0` son (butun bo'lsa `int`).
- Noma'lum `VISSIM_*` o'zgaruvchi (masalan, `VISSIM_ALLOWED_DIR` xatosi) — ogohlantirish va taklif.
- Standart log/audit katalogi: `%LOCALAPPDATA%\vissim-mcp\logs|audit`. Log fayli `vissim-mcp.log` (5 MB × 5),
  bir nechta server bir faylga yozsa ham (Windows'da aylantirish imkonsiz bo'lsa) xato chiqmaydi.

## `mcp` SDK tekshiruvi (PLAN 3.2, 6.1 — M1 natijasi)

O'rnatilgan versiya: **`mcp` 1.30.0** (`pyproject.toml`: `mcp>=1.10,<2`).

- `mcp.server.stdio.stdio_server(stdin=None, stdout=None)` — **ikkala parametr ham mavjud** (anyio `AsyncFile`
  ko'rinishidagi oqimlar). Shuning uchun PLAN 6.1 dagi **asosiy yo'l** ishlatildi: `stdio_guard.install()`
  fd 1 ni stderr'ga yo'naltiradi va himoyalangan asl stdout nusxasini qaytaradi; server shu nusxa ustidagi o'z
  yozuvchisini (`ProtocolWriter`: UTF-8, faqat LF, qisman yozuvlarda ham to'liq yozadi) `stdout=` parametri orqali
  beradi. Muqobil yo'l (`sys.stdout` ni vaqtincha almashtirish) kerak emas.
- stdin: `stdin=` parametriga EOF'ni sezadigan o'quvchi beriladi; stdin yopilganda shu oqimning o'zida
  `service.shutdown_fast()`, log flush va `os._exit(0)` bajariladi (SDK task group'i kutilmaydi). Watchdog
  (250 ms) chiqishni har qanday holatda kafolatlaydi.
- `FastMCP.tool(name=, title=, description=, annotations=, structured_output=)` mavjud; barcha toollar
  `structured_output=False` bilan ro'yxatdan o'tadi va `CallToolResult` qaytaradi (SDK uni o'zgartirmasdan
  uzatadi). `ToolAnnotations` qo'llab-quvvatlanadi (`readOnlyHint`, `destructiveHint`, ...).
- `FastMCP.run_stdio_async()` ichida `stdio_server()` + `_mcp_server.run(...)` — server xuddi shuni o'z oqimlari
  bilan qiladi; `serverInfo.version` sifatida server versiyasi beriladi.
- Kuzatuv: Windows'da `import mcp` SDK'ning stdio *client* yordamchilari orqali `pywintypes`, `win32api`,
  `win32con`, `win32job` ni import qiladi (COM emas: `pythoncom`/`win32com` import qilinmaydi). Testlarda soxta
  `pywintypes` bilan ham SDK to'g'ri ishlashi tekshirilgan.
- Kuzatuv: lone surrogate'li satr (buzilgan COM satri) SDK serializatorini (pydantic) yiqitadi —
  `output.dumps()` bunday belgilarni U+FFFD bilan almashtiradi.

## Kompyuter qayta yoqilgandan keyin

Server — oddiy stdio jarayon: uni Claude Code har sessiya boshida `.mcp.json` dagi buyruq bo'yicha o'zi ishga
tushiradi va sessiya tugaganda yopadi. Shuning uchun Windows xizmati, avtoyuklash yoki qo'lda ishga tushirish kerak
emas — kompyuter qayta yoqilgandan keyin ham Claude Code ochilishi bilan server ishlaydi (venv yo'li o'zgarmasa).
Server Vissim'siz ham tez (~0,5 s) ishga tushadi; COM kerak bo'lgan toollar `vissim_connect` gacha
`NOT_CONNECTED` qaytaradi. To'liq ulanish yo'riqnomasi va misollar M7 da (`examples/`) qo'shiladi.

## Hal qilinmagan savollar (PLAN.md 2-bo'lim)

Javob bo'lmagan savollarda standart qiymatlar ishlatilmoqda — batafsil: [`docs/preflight-report.md`](docs/preflight-report.md)
(Q2: litsenziyada COM yo'q — to'xtatuvchi; Q7: ma'lumot siyosati — ochiq).
