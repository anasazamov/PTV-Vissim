# vissim-mcp

PTV Vissim'ni COM interfeysi orqali boshqarish uchun **stdio MCP server** (Python). Claude Code (Windows'dagi
Desktop ilovasi yoki CLI) shu server orqali Vissim'ni ishga tushiradi, tarmoq yuklaydi, atributlarni o'qiydi/yozadi
va simulyatsiyani fon job sifatida yurgizadi. Loyihaning yagona manbai — [`PLAN.md`](PLAN.md).

## Holat

| Bosqich | Holat |
|---|---|
| M0 — Pre-flight | bajarildi ([`docs/preflight-report.md`](docs/preflight-report.md), [`docs/com-names.md`](docs/com-names.md)) |
| M1 — Skelet | bajarildi: konfiguratsiya, xato kodlari, log, stdout himoyasi, chiqish cheklovlari, soxta (fake) COM qatlami, testlar, CI |
| **M2 — Worker va hayot sikli** | **bajarildi**: COM worker (bitta STA oqim), `service.call` watchdog'i, Vissim jarayonini kuzatish (PID, Job Object), `vissim_connect`, `vissim_health`, `vissim_info`, `vissim_restart`, `vissim_close` toollari |
| M3–M7 | hali bajarilmagan (fayllar va obyektlar, simulyatsiya, signallar, haqiqiy Vissim'da tekshirish, reliz) |

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
  `notifications/initialized` → `tools/list` → `tools/call` ketma-ketligini o'tkazadi. stdout'dagi
  **har bir qator** JSON-RPC ekani tekshiriladi — shu jumladan ataylab `print()`, `sys.stdout`/`sys.__stdout__`,
  `os.write(1, ...)` va Windows'da `WriteFile(GetStdHandle(STD_OUTPUT_HANDLE))` orqali "shovqin" qilingan holatda
  (`tests/protocol/noisy_launcher.py`; production kodida test uchun maxsus ilgak yo'q).
- stdin yopilgandan keyin jarayon **300 ms** ichida chiqishi o'lchanadi (o'lchov `stdin.close()` dan oldin
  boshlanadi; CI uchun qo'shimcha "zaxira" berilmagan). Mahalliy o'lchov (30 marta): 6–7 ms; COM worker osilib
  qolgan holatda (soxta `Dispatch` yoki tool chaqiruvi bloklangan, 8 marta): 110–125 ms; stand-in jarayon
  ulangan va tool chaqiruvi osilib qolgan holatda (stdin yopilganda jarayon haqiqatan o'ldiriladi): 112–124 ms.
- `tests/unit/test_worker.py`, `test_service_lifecycle.py`, `test_winproc.py`, `test_lifecycle_tools.py`,
  `test_backend.py` (M2) — PLAN 10.1 dagi worker/service/winproc qatorlari: timeout → `TIMEOUT_HUNG`, timeout'dan
  keyin `health.busy_seconds > 0`, restart → `generation` +1 va ishlayotgan job `aborted`, simulyatsiya bo'laklari
  orasida navbat to'liq bo'shatilishi, 0/1/2 ta yangi PID, PID noma'lum bo'lsa kill umuman chaqirilmasligi, PID
  qayta ishlatilishi (identity tekshiruvi), litsenziya oynasi, har bir tool uchun baxtli va xato yo'li. Jarayonlar
  `tests/fake_procs.py` (`FakeProcs`, soxta jarayon jadvali) orqali — unit testlar haqiqiy jarayonga tegmaydi.
- Windows'da Job Object haqiqiy OS bilan tekshiriladi: zararsiz `python -c "import time; time.sleep(60)"` jarayoni
  yordamchi jarayon ushlab turgan kill-on-close job'ga qo'shiladi, yordamchi o'ldiriladi va bola jarayon ham
  tugashi tasdiqlanadi (nazorat: job'siz bola tirik qoladi). Vissim hech qachon ishlatilmaydi.
- `tests/protocol/test_lifecycle_protocol.py` (M2) — `connect → health → info → restart → close` haqiqiy backend
  kodi va soxta pywin32 modullari orqali, har bir soxta COM chaqiruvi stdout'ga "shovqin" qilgan holatda
  (`FAKE_VISSIM_PRINT_STDOUT=1`); osilib qolgan `Dispatch` (`FAKE_VISSIM_HANG_S`) yoki tool chaqiruvi paytida stdin
  yopilsa ham chiqish ≤ 300 ms. Bu testlarda `VISSIM_EXE_PATTERN` hech bir haqiqiy dasturga mos kelmaydigan
  nomga qo'yiladi — server haqiqiy jarayonlarni o'qimaydi ham.
- `tests/protocol/test_standin_protocol.py` (faqat Windows) — haqiqiy jarayon zanjiri server ichida: soxta
  `Dispatch` zararsiz "stand-in" jarayonini ochadi (asosiy `python.exe` nusxasi, nomi
  `vissim_mcp_test_standin.exe`, buyruq qatorida `-Embedding`; nomi haqiqiy `Vissim*.exe` ga hech qachon mos
  kelmaydi). Tekshiriladi: psutil orqali PID aniqlash, kill-on-close Job Object, `vissim_restart` eski nusxani
  o'ldiradi, `vissim_close`, stdin yopilganda nusxa o'ldirilib server ≤ 300 ms da chiqadi, server keskin
  o'ldirilganda (stdin EOF'siz) nusxani Job Object yopadi (nazorat: Job Object'siz nusxa tirik qoladi).
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
- `tests/fake_backend.py` — `FakeBackend` (in-process unit testlar uchun) va `BackendFactory` (har bir
  `Dispatch` `FakeProcs` ichida `-Embedding` bilan soxta Vissim jarayonini "ishga tushiradi"; jarayon o'ldirilsa,
  osilib qolgan soxta COM chaqiruvi haqiqiy COM kabi xato bilan tugaydi).

## Hayot sikli toollari (M2, PLAN 6.2–6.4, 7.1)

- `vissim_connect` — har doim **yangi** Vissim nusxasini COM orqali ishga tushiradi (foydalanuvchi ochgan oynaga
  ulanmaydi). `Dispatch` worker oqimida bajariladi; kutish paytida server yangi `-Embedding` jarayonlarni va
  ularning oynalarini kuzatadi: litsenziya oynasi ("License not found", "Управление лицензиями") chiqsa — faqat
  o'zi ochgan **yagona** yangi jarayonni yopadi va tushunarli `COM_ERROR` qaytaradi; `VISSIM_CONNECT_TIMEOUT_S`
  o'tsa — `TIMEOUT_HUNG`. Muvaffaqiyatda PID aniqlanadi (PLAN 6.4) va jarayon kill-on-close Job Object'ga
  biriktiriladi. Egalik isboti — buyruq qatoridagi `-Embedding` (M0: COM ochgan Vissim'da u doim bor):
  - 1 ta yangi jarayon, buyruq qatorida `-Embedding` bor → o'sha PID;
  - 1 ta yangi jarayon, buyruq qatorini o'qib bo'lmadi (AccessDenied) → PLAN 6.4 bo'yicha o'sha PID, lekin
    "egalik tasdiqlanmadi" ogohlantirishi bilan;
  - 1 ta yangi jarayon, buyruq qatori o'qildi, lekin `-Embedding` yo'q (foydalanuvchi o'sha paytda o'zi ochgan
    oyna) → **qabul qilinmaydi**: `pid=null`, Job Object yo'q, ogohlantirish "a new Vissim process without
    -Embedding appeared during connect; it was not adopted"; bunday jarayon hech qachon o'ldirilmaydi;
  - bir nechta → eng yangi `-Embedding` li (u bo'lmasa — buyruq qatori o'qilmagan eng yangisi) + ogohlantirish;
    `-Embedding` siz jarayon tanlanmaydi; 0 → `pid=null` + ogohlantirish.
- `vissim_health` — COM'siz, darhol javob beradi: ulanish, PID, `generation`, `busy_seconds`/`current_task`
  (osilib qolgan COM chaqiruvi), navbat, job, sozlamalar, oxirgi xato.
- `vissim_info` — asosiy kolleksiyalar soni (har biri alohida o'qiladi; o'qib bo'lmasa `null` + `errors`),
  `SimPeriod`, `SimRes`.
- `vissim_restart` — o'z Vissim'ini o'ldiradi, eski worker'ni tashlab yuboradi (`generation` +1, uning kech
  kelgan natijalari e'tiborsiz), ishlayotgan job'ni `aborted` qiladi va yangidan ulanadi. Modal dialog tufayli
  osilib qolgan yoki qulagan Vissim'ni MCP serverni qayta ishga tushirmasdan tiklaydi. Eski Vissim o'ldirilmagan
  bo'lsa (PID noma'lum, kill rad etildi — masalan, Vissim administrator sifatida ishlayapti — yoki kill'dan keyin
  ham tirik) — ogohlantirish beriladi va `vissim_health` da ham ko'rinadi. Yangi ishga tushirish muvaffaqiyatsiz
  bo'lsa, xato `details` ida eski Vissim haqidagi ma'lumot qoladi (`previous_pid`, `previous_killed`,
  `aborted_job_id`, `previous_warnings`); eski nusxa hali ishlayotgan bo'lishi mumkin bo'lsa, xabarda aytiladi
  (bitta litsenziya seat'ida aynan u yangi nusxaga xalaqit beradi).
- `vissim_close` — `Exit()` → 10 s gacha kutish → `terminate` → `kill`; javobda `method`. Saqlash parametri yo'q.
  `Exit()` COM xatosi bilan tugasa ham (masalan, `RPC_S_CALL_FAILED` — Vissim yopilayotganda javob yo'lda
  yo'qoladi) jarayon avval 10 s kutiladi, ya'ni normal yopilayotgan Vissim majburan tugatilmaydi; `Exit()` javob
  bermay turganda jarayon tugasa — darhol `method: "exit"`. PID noma'lum bo'lsa server hech narsani tugatmaydi va
  buni ochiq aytadi. Chaqiruv bekor qilinsa (masalan, "Save changes?" dialogi paytida) — ulanish "uzilgan"
  holatda qoladi (worker tashlab yuboriladi, PID saqlanadi): keyingi `vissim_close`, `vissim_connect`,
  `vissim_restart` yoki server yopilishi o'sha Vissim'ni yopadi.
- Har bir COM chaqiruvi watchdog bilan (`VISSIM_CALL_TIMEOUT_S`): muddat o'tsa `TIMEOUT_HUNG` ("Vissim'da modal
  dialog ochiq bo'lishi mumkin — oynani tekshiring yoki `vissim_restart`"). COM xatolari HRESULT bo'yicha
  `COM_ERROR` ga aylanadi; ulanish uzilgani (`RPC_E_DISCONNECTED` va h.k. yoki Vissim jarayoni yo'qolgani)
  aniqlansa, `connected=false` va `vissim_restart` tavsiya qilinadi. `RPC_S_CALL_FAILED` (chaqiruv yo'lda
  yo'qoldi) o'zi uzilish isboti emas: PID ma'lum bo'lsa, jarayon qisqa vaqt (1 s) kuzatiladi va tugagan bo'lsa
  ulanish uzilgan deb belgilanadi; PID noma'lum bo'lsa — keyingi chaqiruv aniqlaydi. Vissim jarayoni yo'qolgan
  "ulangan" holatda `vissim_connect` `ALREADY_CONNECTED` bermaydi — eski ulanishni tashlab, yangisini ochadi.
- Simulyatsiya bo'lagi (`tick`, M4) xato o'rniga istisno ko'tarsa, job bir marta `error` holatiga o'tkaziladi va
  qayta "tick" qilinmaydi (traceback log'ga bir marta yoziladi) — har 50 ms takrorlanadigan xato sikli bo'lmaydi.

Xavfsizlik qoidalari:

- Server faqat o'ziniki deb qabul qilingan jarayonni o'ldiradi: `Dispatch` dan oldingi/keyingi jarayonlar farqi +
  `VISSIM_EXE_PATTERN` + buyruq qatoridagi `-Embedding` (o'qib bo'lmasa — ogohlantirish bilan, yuqoriga qarang) +
  ishga tushgan vaqt (PID qayta ishlatilishidan himoya). PID aniqlanmasa yoki jarayon qabul qilinmasa — hech narsa
  o'ldirilmaydi va Job Object ishlatilmaydi. Ulanish xatosidagi tozalash `-Embedding` ni albatta talab qiladi va
  faqat bitta nomzod bo'lsa ishlaydi.
- stdin yopilganda (Claude Code sessiyasi tugaganda): yangi vazifalar rad etiladi → `VISSIM_KILL_ON_EXIT=1` va
  PID ma'lum bo'lsa Vissim darhol o'ldiriladi → worker'ga 100 ms dan ortiq kutilmaydi → log flush → chiqish.
  Server qanday o'lmasin (hatto jarayon daraxti o'ldirilsa ham), Job Object Vissim'ni Windows orqali yopadi.
  Job faqat `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (0x2000) bilan yaratiladi (PLAN 6.4): Vissim ochgan barcha
  jarayonlar job ichida qoladi va u bilan birga tugaydi; `CREATE_BREAKAWAY_FROM_JOB` bilan ochilmoqchi bo'lgan
  jarayonni Windows rad etadi. Haqiqiy Vissim bunga muhtojmi — M6 da tekshiriladi
  ([`docs/com-names.md`](docs/com-names.md)).
- Test rejimida (`VISSIM_MCP_FORBID_REAL=1`) haqiqiy `Vissim*.exe` hech qachon o'ldirilmaydi va Job Object'ga
  qo'shilmaydi, haqiqiy COM `Dispatch` esa rad etiladi.

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
