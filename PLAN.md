# vissim-mcp — PTV Vissim uchun MCP server: amalga oshirish rejasi

> **Bu hujjat kimga:** Claude Code (ijrochi) va loyiha egasi (nazoratchi).
> **Qanday ishlatiladi:** fayl repozitoriya ildiziga `PLAN.md` nomi bilan qo'yiladi. Claude Code har bir bosqichni (M0–M7) alohida sessiyada bajaradi; har bosqich oxirida "Tayyorlik mezoni" (DoD) tekshiriladi va faqat shundan keyin keyingisiga o'tiladi. 13-bo'limda har bir bosqich uchun tayyor promptlar bor.
> **Til:** hujjat o'zbekcha; kod, identifikatorlar, tool tavsiflari (docstring), log xabarlari va commit'lar — inglizcha.
> **Versiya:** 1.0 — 2026-10-16

---

## 0. Qisqacha

Maqsad: Claude Code (Windows'dagi Desktop ilovasi yoki CLI) PTV Vissim'ni COM interfeysi orqali **ishonchli** boshqara olishi uchun Python'da stdio MCP server yozish.

Asosiy tamoyillar (o'zgarmas):

1. **Bitta COM oqimi.** Vissim COM obyekti faqat bitta maxsus STA oqimda yaratiladi va chaqiriladi. Boshqa hech qayerda emas.
2. **Uzoq ish — fon job.** Simulyatsiya hech qachon bitta sinxron tool chaqiruvi ichida oxirigacha yurmaydi. `start` darhol `job_id` qaytaradi, holat alohida so'raladi.
3. **stdout — faqat MCP protokoli.** Fayl deskriptori darajasida himoyalanadi.
4. **Fail-closed xavfsizlik.** Ruxsat etilgan katalog sozlanmagan bo'lsa, fayl operatsiyalari rad etiladi. Yozish va saqlash har doim audit qilinadi.
5. **Tasdiqlanmagan API nomi kodga qattiq yozilmaydi.** Bunday nomlar konfiguratsiya yoki umumiy `get/set` tool orqali beriladi va `docs/com-names.md` jadvalida holati yuritiladi.
6. **Haqiqiy Vissim'siz test qilinadi.** Soxta (fake) COM qatlami orqali barcha mantiq Linux/Windows CI'da tekshiriladi; haqiqiy Vissim testlari alohida belgi bilan.

---

## 1. Doira

### 1.1 v1.0 ga kiradi

- Vissim'ni ishga tushirish, holatini tekshirish, majburan qayta ishga tushirish, yopish.
- `.inpx` / `.layx` yuklash, zaxira bilan saqlash.
- Istalgan obyekt va kolleksiya atributlarini o'qish (umumiy yo'l sintaksisi), yozish (`dry_run` bilan), ommaviy yozish.
- Obyekt a'zolarini aniqlash (COM type info orqali, "best effort").
- Simulyatsiya sozlash; ko'p runli fon simulyatsiya; to'xtatish; belgilangan soniyada pauza va davom ettirish; chegaralangan kutish.
- Natijalarni sahifalab o'qish va CSV'ga eksport qilish.
- Signal guruhi holatini COM orqali qo'yish va kontrollerga qaytarish.
- Audit log, server log, log'ning oxirini o'qish tooli.
- Standart holatda o'chiq `exec` tooli (faqat test kompyuterida).

### 1.2 v1.0 ga kirmaydi (non-goals)

- Foydalanuvchi o'zi ochgan Vissim oynasiga ulanish (attach). v1.0 doim o'zining yangi nusxasini ochadi.
- Bir vaqtda bir nechta Vissim nusxasi (litsenziya tasdiqlanmagan).
- Simulyatsiya qadamida real vaqtli adaptiv signal boshqaruvi (Claude har qadamda chaqiradigan sikl). Bu v1.1+ da server ichidagi strategiya sifatida.
- DriverModel.dll, SignalControl.dll, Viswalk.
- Windows xizmati (Session 0) sifatida ishlash.
- HTTP transport. Faqat stdio.

---

## 2. Ishni boshlashdan oldin hal qilinadigan savollar

Loyiha egasi javob beradi. Javob bo'lmasa, Claude Code "standart" ustunidagi qiymat bilan ishlaydi va buni README'da qayd etadi.

| # | Savol | Standart (javob bo'lmasa) |
|---|---|---|
| Q1 | O'rnatilgan Vissim versiyasi(lari)? (2024 / 2025 / 2026) | Bitta versiya; versiyasiz ProgID `Vissim.Vissim` |
| Q2 | Litsenziya turi (single-user / floating / PTV Hub) va unda COM moduli bormi? | COM bor deb faraz qilinadi; M0 da tekshiriladi |
| Q3 | Claude Code va Vissim bitta Windows kompyuterdami? | Ha (bu majburiy shart) |
| Q4 | Loyihalar qaysi katalogda? Asl modellar qaysi papkada? | `D:\VissimProjects`, asl modellar `models\original\` |
| Q5 | Claude Code sessiyasi tugaganda Vissim majburan yopilsinmi (seat bo'shatish)? | Ha (`VISSIM_KILL_ON_EXIT=1`) |
| Q6 | Simulyatsiya paytida GUI ko'rinib tursinmi yoki maksimal tezlikmi? | Maksimal tezlik (QuickMode, GUI yangilanishi to'xtatilgan) |
| Q7 | Tarmoq ma'lumotlarini (geometriya, hajmlar) Anthropic API'ga yuborish Kompaniya siyosatiga to'g'rimi? | Yuridik bo'lim tasdig'igacha faqat test modellari bilan ishlanadi |

> Q7 bo'yicha eslatma: Claude Code MCP tool natijalarini model kontekstiga yuboradi. Server faqat so'ralgan ustunlarni qaytaradi, katta jadvallar lokal CSV'da qoladi, lekin bu masalaning o'zini hal qilmaydi — qaror tashkilot darajasida.

---

## 3. Muhit va oldindan shartlar

### 3.1 Majburiy

| Talab | Tekshirish usuli |
|---|---|
| Windows 10/11, interaktiv foydalanuvchi sessiyasi | — |
| Vissim 2024+ (64-bit), litsenziyada **COM moduli** | Vissim → Help → License. Demo/Student'da COM yo'q; Junction'da optional |
| COM server ro'yxatdan o'tgan | Vissim'ni administrator sifatida ochish → Help → Register COM Server |
| Litsenziya "default" qilib saqlangan (ishga tushishda tanlash dialogi chiqmasin) | Vissim'ni oddiy ochib ko'rish: dialogsiz ochilishi kerak |
| Python 3.11+ **64-bit**, alohida venv | `py -3.12 -c "import struct;print(struct.calcsize('P')*8)"` → `64` |
| Claude Code — Windows'da native (WSL emas) | `claude --version` (v2.1.212+ tavsiya — avto-fon uchun) |

### 3.2 Bog'liqliklar (`pyproject.toml`)

- Runtime: `mcp>=1.10,<2`, `pywin32>=306` (faqat `sys_platform == "win32"`), `psutil>=5.9`.
- Dev: `pytest>=8`, `pytest-asyncio>=0.23`, `ruff`, `mypy` (ixtiyoriy).
- `mcp` SDK versiyasi qat'iy pastki/yuqori chegara bilan; M1 da o'rnatilgan versiyaning `stdio_server` va `FastMCP` API'lari tekshirilib, README'ga yoziladi.

### 3.3 Claude Code xatti-harakati (rasmiy hujjat bo'yicha tekshirilgan, 2026-10)

Dizayn shularga tayanadi:

- Per-server `"timeout"` (ms) — har bir tool chaqiruvining qat'iy wall-clock chegarasi; progress bildirishnomalari uni uzaytirmaydi; < 1000 qiymatlar e'tiborsiz.
- `MCP_TOOL_TIMEOUT` o'rnatilmasa ~28 soat.
- Stdio serverlar uchun idle timeout 30 daqiqa; per-server `timeout` ≥ 1000 bo'lsa, u idle uchun quyi chegara ham bo'ladi.
- Asosiy suhbatdagi chaqiruv 2 daqiqadan oshsa fon vazifasiga o'tadi (v2.1.212+); **subagent chaqiruvlari fonga o'tmaydi**.
- Chiqish > 10 000 token — ogohlantirish; standart chegara 25 000 token; 50 000 belgidan uzun matnli natija faylga saqlanadi.
- Windows'da to'xtatish: stdin yopiladi, 300 ms dan keyin jarayon daraxti tugatiladi (v2.1.296+).
- Stdio serverlar avtomatik qayta ulanmaydi (`/mcp` orqali qo'lda).
- Claude Code server muhitiga `CLAUDE_PROJECT_DIR` ni qo'yadi.

**Dizayndan kelib chiqadigan qoidalar:**

- Har qanday tool ≤ 90 soniyada javob qaytarishi maqsad qilinadi (2 daqiqalik avto-fon chegarasidan pastda). Faqat `connect`, `load_network` istisno bo'lishi mumkin.
- Vissim DCOM orqali ishga tushadi va server jarayon daraxtida **emas** — Claude Code uni o'ldirmaydi. Shuning uchun Vissim'ni yopish mexanizmi serverning o'z vazifasi (6.4-bo'lim).
- 300 ms ichida `Exit()` ni kutib bo'lmaydi → yopilish majburiy kill + Job Object.

---

## 4. Arxitektura

### 4.1 Umumiy sxema

```
Claude Code (Windows)
   │  stdio (JSON-RPC)
   ▼
┌───────────────────────────── vissim-mcp (python.exe) ─────────────────────────────┐
│ main thread: asyncio + FastMCP                                                    │
│   tools/*.py  ──►  service.call(fn, timeout)  ──►  queue  ──►  ComWorker (STA)    │
│        ▲                                   asyncio.wrap_future        │           │
│        │  status/health (COM'siz, worker holatidan o'qiydi)           │           │
│        └──────────────────────────────────────────────────────────────┤           │
│                                                                       ▼           │
│                                             backend: RealComBackend | FakeBackend │
│ safety: PathPolicy, AttributePolicy, Audit, OutputLimiter                         │
│ winproc: PID detection, Job Object (kill-on-close), kill                          │
│ stdio_guard: fd 1 → stderr; MCP uchun alohida himoyalangan kanal                  │
└───────────────────────────────────────────────────────────────────────────────────┘
   │  DCOM (out-of-process)
   ▼
Vissim.exe  ── Job Object ichida (server o'lsa, OS uni ham yopadi)
```

### 4.2 Repozitoriya tuzilmasi

```
vissim-mcp/
├─ PLAN.md                      # shu hujjat
├─ README.md                    # o'rnatish va ishlatish (o'zbekcha)
├─ pyproject.toml
├─ src/vissim_mcp/
│  ├─ __init__.py               # __version__
│  ├─ __main__.py               # python -m vissim_mcp
│  ├─ stdio_guard.py            # fd-darajadagi stdout himoyasi (import'dan OLDIN ishlaydi)
│  ├─ config.py                 # env → Settings dataclass, validatsiya
│  ├─ errors.py                 # ErrorCode enum, VissimMcpError, com_error → kod
│  ├─ logging_setup.py          # rotating file log + stderr
│  ├─ audit.py                  # JSONL audit yozuvchi
│  ├─ policy.py                 # PathPolicy, AttributePolicy
│  ├─ output.py                 # to_jsonable, qator/belgi cheklovlari
│  ├─ winproc.py                # PID topish, Job Object, kill (Windows'dan tashqarida no-op)
│  ├─ backend/
│  │  ├─ base.py                # ComBackend Protocol: dispatch(progid), co_init/uninit, pump
│  │  └─ real.py                # pythoncom + win32com.client.dynamic
│  ├─ worker.py                 # ComWorker: navbat, oqim, ulanish hayot sikli
│  ├─ resolver.py               # "Net.SignalControllers[1].SGs" → COM obyekt
│  ├─ jobs.py                   # SimulationJob holat mashinasi
│  ├─ service.py                # VissimService: call(), restart(), shutdown()
│  ├─ server.py                 # FastMCP ilovasi, tool registratsiyasi, run()
│  └─ tools/
│     ├─ lifecycle.py           # connect, health, restart, close, info
│     ├─ files.py               # load_network, save_network
│     ├─ objects.py             # describe, list_objects, get/set_attributes, bulk
│     ├─ simulation.py          # configure, start, status, wait, pause/resume, stop
│     ├─ results.py             # export_csv
│     ├─ signals.py             # set_signal_state
│     └─ diagnostics.py         # tail_log, exec_python
├─ tests/
│  ├─ fakes/                    # soxta pythoncom, pywintypes, win32com.client.dynamic
│  ├─ fake_vissim.py            # FakeVissim modeli (6.10-bo'lim)
│  ├─ unit/                     # pytest, haqiqiy Vissim'siz
│  ├─ protocol/                 # serverni subprocess sifatida stdio orqali sinash
│  └─ real/                     # @pytest.mark.vissim — faqat Vissim'li kompyuterda
├─ scripts/
│  ├─ preflight.ps1             # M0 tekshiruvlari
│  └─ probe_com_names.py        # tasdiqlanmagan nomlarni haqiqiy Vissim'da sinash
├─ docs/
│  ├─ com-names.md              # API nomlari va ularning tasdiq holati jadvali
│  └─ troubleshooting.md
└─ examples/
   ├─ .mcp.json.example
   ├─ settings.json.example     # Claude Code ruxsatlari
   └─ CLAUDE.md.example         # Vissim loyihasi uchun qoidalar
```

### 4.3 Qatlamlar o'rtasidagi qoidalar

- `tools/*` hech qachon COM obyektini to'g'ridan-to'g'ri ushlamaydi; faqat `service.call(fn)` ga funksiya beradi. `fn` worker oqimida bajariladi va **faqat JSON'ga aylantiriladigan qiymat** qaytaradi (COM obyekt oqimdan tashqariga chiqmaydi).
- `worker.py` va `jobs.py` `pythoncom` / `win32com` ni import qilmaydi — faqat `backend` orqali. Shu tufayli fake backend bilan to'liq test qilinadi.
- `winproc.py` Windows bo'lmagan platformada barcha funksiyalarni xavfsiz no-op qiladi.

---

## 5. Konfiguratsiya (muhit o'zgaruvchilari)

Hammasi `config.py` da bitta `Settings` dataclass'ga o'qiladi, ishga tushishda validatsiya qilinadi va (maxfiy bo'lmagani uchun) `vissim_health` da ko'rsatiladi.

| O'zgaruvchi | Standart | Ma'nosi |
|---|---|---|
| `VISSIM_PROGID` | `Vissim.Vissim` | COM ProgID. Bir nechta versiya bo'lsa — versiyaga xos (registrdan aniqlanadi) |
| `VISSIM_ALLOWED_DIRS` | (bo'sh) | `;` bilan ajratilgan ruxsat etilgan ildizlar. Bo'sh bo'lsa → `CLAUDE_PROJECT_DIR`. Ikkalasi ham yo'q bo'lsa → **barcha fayl operatsiyalari rad** |
| `VISSIM_READONLY_DIRS` | `<har bir ildiz>\models\original` | Bu yerlarga yozish/saqlash taqiqlanadi |
| `VISSIM_CALL_TIMEOUT_S` | `120` | Oddiy COM vazifasi uchun watchdog |
| `VISSIM_CONNECT_TIMEOUT_S` | `180` | Vissim ishga tushishi uchun |
| `VISSIM_LOAD_TIMEOUT_S` | `600` | Tarmoq yuklash uchun |
| `VISSIM_CHUNK_SIM_S` | `60` | Bir bo'lakdagi simulyatsiya soniyalari |
| `VISSIM_WAIT_MAX_S` | `90` | `vissim_wait_simulation` ning yuqori chegarasi (2 daqiqadan past) |
| `VISSIM_EXE_PATTERN` | `^vissim\d*\.exe$` (case-insensitive) | Vissim jarayon nomi regex'i |
| `VISSIM_PID_DETECT_S` | `20` | Yangi PID'ni kutish muddati (polling) |
| `VISSIM_KILL_ON_EXIT` | `1` | Server tugaganda Vissim'ni majburan yopish |
| `VISSIM_USE_JOB_OBJECT` | `1` | Vissim'ni kill-on-close Job Object'ga biriktirish |
| `VISSIM_MAX_ROWS` | `500` | Bitta javobdagi maksimal qator |
| `VISSIM_MAX_RESULT_CHARS` | `40000` | Bitta javobning maksimal JSON hajmi (50 000 chegarasidan past) |
| `VISSIM_ALLOW_PATH_ATTRS` | `0` | Nomida File/Path/Folder/Dir bo'lgan atributlarni yozishga ruxsat |
| `VISSIM_MCP_ALLOW_EXEC` | `0` | `vissim_exec_python` ni yoqish |
| `VISSIM_LOG_DIR` | `%LOCALAPPDATA%\vissim-mcp\logs` | Server log |
| `VISSIM_AUDIT_DIR` | `%LOCALAPPDATA%\vissim-mcp\audit` | Audit JSONL |
| `VISSIM_SEED_ATTR` | `RandSeed` (*) | Tasodifiy urug' atributi nomi — tasdiqlanmagan, shuning uchun sozlanadi |
| `VISSIM_BREAK_DISABLE_VALUE` | `0` (*) | "To'xtash nuqtasi yo'q" qiymati — tasdiqlanmagan |

(*) — `docs/com-names.md` da "tasdiqlanmagan" holatida; M6 da haqiqiy Vissim'da tekshiriladi.

---

## 6. Asosiy mexanizmlar — batafsil spetsifikatsiya

### 6.1 stdout himoyasi (`stdio_guard.py`)

**Muammo:** stdout'ga tushgan har qanday bayt JSON-RPC oqimini buzadi. `contextlib.redirect_stdout` jarayon bo'yicha global va C/DLL darajasidagi yozuvlarni ushlamaydi.

**Yechim** — `__main__.py` ning eng birinchi qatorida, boshqa hech narsa import qilinmasdan:

1. `proto_fd = os.dup(1)` — asl stdout'ning nusxasi; faqat MCP shu orqali yozadi.
2. `os.dup2(2, 1)` — fd 1 endi stderr'ga ishora qiladi. Windows'da qo'shimcha: `SetStdHandle(STD_OUTPUT_HANDLE, get_osfhandle(2))` (ctypes).
3. `sys.stdout = sys.stderr` — Python darajasidagi `print()` ham stderr'ga.
4. `proto_out = open(proto_fd, "wb", buffering=0)` ni `server.run()` ga uzatish; u `mcp.server.stdio.stdio_server(stdout=anyio.wrap_file(TextIOWrapper(proto_out, encoding="utf-8", newline="\n")))` bilan ishlatiladi.
5. Agar o'rnatilgan `mcp` versiyasida `stdio_server` `stdout` parametrini qabul qilmasa — M1 da aniqlanadi va muqobil yo'l (`sys.stdout` ni faqat `stdio_server()` ga kirish paytida vaqtincha almashtirish) README'da qayd etiladi.
6. stdin ham UTF-8: `TextIOWrapper(sys.stdin.buffer, encoding="utf-8")`.

**Test:** protocol testi stdout'dagi har bir qator JSON ekanini tekshiradi, shu jumladan fake backend ataylab `print()` va `os.write(1, ...)` qiladigan holatda.

### 6.2 COM worker (`worker.py`)

- Bitta `threading.Thread(name="vissim-com", daemon=True)`.
- Oqim boshida `backend.co_initialize()`, oxirida barcha havolalar `None` qilingandan keyin `backend.co_uninitialize()`.
- Asosiy sikl:
  ```
  loop:
      item = queue.get(timeout=0.05)
      if item: bajar → Future'ga natija/xato; continue      # avval navbatni bo'shatish
      backend.pump_messages()
      if job.running and not job.paused: job.tick()          # bitta bo'lak
  ```
  Ya'ni har bir bo'lakdan keyin navbat to'liq bo'shatiladi (adolatlilik), shundan keyin keyingi bo'lak.
- Har vazifa uchun `busy_since`, `current_task_name` yoziladi — `health` shuni ko'rsatadi.
- Worker `generation` raqamiga ega. `restart` yangi worker yaratadi; eski worker'ning kech kelgan natijalari e'tiborsiz qoldiriladi.
- `submit()` worker to'xtatilgan bo'lsa darhol `NOT_CONNECTED` xatosi bilan Future qaytaradi.

### 6.3 `service.call()` va watchdog

```python
async def call(fn, *, timeout_s, task_name) -> Any
```
- `asyncio.wait_for(asyncio.wrap_future(worker.submit(fn)), timeout_s)`.
- Timeout bo'lsa: `TIMEOUT_HUNG` xatosi; xabarda "Vissim'da modal dialog ochiq bo'lishi mumkin — oynani tekshiring yoki `vissim_restart` chaqiring". Worker band holatda qoladi (COM chaqiruvini to'xtatib bo'lmaydi) — keyingi chaqiruvlar ham timeout'ga tushadi, `health` esa `busy_seconds` ni ko'rsatadi.
- COM xatosi (`pywintypes.com_error`) `errors.from_com_error()` orqali `COM_ERROR` ga aylanadi: `hresult`, `message`, `excepinfo` dagi tavsif (odatda 3-element), mavjud bo'lsa `scode`. Ma'lum HRESULT'lar uchun maxsus `hint`:
  - `CO_E_CLASSSTRING` (-2147221005, "Invalid class string") → COM ro'yxatdan o'tmagan.
  - `CO_E_NOTINITIALIZED` (-2147221008) → ichki xato (COM boshqa oqimda chaqirilgan).
  - `RPC_E_DISCONNECTED` / `RPC_S_SERVER_UNAVAILABLE` → Vissim yopilgan yoki qulagan → `vissim_restart`.

### 6.4 Vissim jarayonini kuzatish va yopish (`winproc.py`)

**PID aniqlash** (`connect` ichida):
1. `before = vissim_pids()` — `VISSIM_EXE_PATTERN` ga aniq mos jarayonlar.
2. `Dispatch(progid)`.
3. `VISSIM_PID_DETECT_S` gacha 250 ms oraliq bilan polling: `new = vissim_pids() - before`.
4. `len(new) == 1` → shu PID. `> 1` → eng yangi `create_time`, lekin `warnings` ga yoziladi. `0` → `pid=None`, `warnings: ["PID not detected; forced kill and job object disabled"]`.
5. **Xavfsizlik qoidasi:** PID aniq bo'lmasa, server hech qachon hech qaysi jarayonni o'ldirmaydi.

**Job Object** (`VISSIM_USE_JOB_OBJECT=1`):
- `CreateJobObjectW` → `SetInformationJobObject(JobObjectExtendedLimitInformation, LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)` → `OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE)` → `AssignProcessToJobObject`.
- Job handle server umri davomida ochiq ushlanadi va hech qachon qo'lda yopilmaydi. Server jarayoni qanday o'lmasin (Claude Code daraxtni o'ldirsa ham), OS handle'ni yopadi va Vissim'ni tugatadi.
- Biriktirish muvaffaqiyatsiz bo'lsa (masalan, Vissim administrator sifatida ishlayotgan bo'lsa → access denied) — xato emas, `warnings` ga yoziladi va log'ga tushadi.
- `VISSIM_KILL_ON_EXIT=0` bo'lsa Job Object ishlatilmaydi.

**Yopilish ketma-ketligi** (stdin EOF yoki `mcp.run` qaytganda), ≤ 200 ms byudjet:
1. `shutting_down = True`; yangi vazifalar qabul qilinmaydi.
2. `VISSIM_KILL_ON_EXIT=1` va PID aniq → `psutil.Process(pid).kill()` (kutmasdan).
3. Worker'ga to'xtash signali; 100 ms dan ortiq kutilmaydi (daemon oqim).
4. Log flush.
> Saqlanmagan o'zgarishlar yo'qoladi — shuning uchun saqlash faqat aniq `vissim_save_network` orqali, va CLAUDE.md'da "o'zgarishdan keyin saqlang" qoidasi bor.

**`vissim_close`** (sessiya ichida, vaqt cheklovi yo'q): `Exit()` → 10 s gacha jarayon tugashini kutish → kerak bo'lsa `terminate` → `kill`. Saqlash parametri **yo'q**.

**`vissim_restart`**: aniq PID'ni kill → eski worker'ni tashlab yuborish (generation++) → joriy job'ni `aborted` qilish → yangi worker → `connect`.

### 6.5 Yo'l rezolveri (`resolver.py`)

Sintaksis: `Segment(.Segment)*`, segment = `Name` yoki `Name[key]`.

- `Net.Links[5]` → `vissim.Net.Links.ItemByKey(5)`
- `Net.SignalControllers[1].SGs[2]`
- `Net.VehicleRoutingDecisionsStatic[1].VehRoutSta[2]`
- `Simulation`, `Evaluation`, `Graphics.CurrentNetworkWindow`
- Kalit: butun son bo'lsa `int`, aks holda qo'shtirnoqdagi satr: `Net.Nodes["A1"]`.

Qoidalar:
- Ildiz har doim `Vissim` obyekti; yo'l `Vissim.` bilan boshlanishi ham mumkin (olib tashlanadi).
- Segment nomi `^[A-Za-z][A-Za-z0-9_]*$` ga mos bo'lishi shart — `_` bilan boshlanadigan (`_oleobj_` kabi) va metod chaqiruvi bo'lgan yo'llar rad etiladi.
- Yo'l parsingi COM'siz alohida funksiya (`parse_path`) — unit test bilan to'liq qoplanadi.
- Xato xabari qaysi segmentda to'xtaganini aytadi: `"Segment 'SGs' on 'Net.SignalControllers[1]': <COM xato>"`.

### 6.6 Simulyatsiya job'i (`jobs.py`) — holat mashinasi

**Holatlar:** `running` → `paused` ⇄ `running` → `finished` | `stopped` | `error` | `aborted`.

**Job maydonlari:** `id`, `state`, `run`, `runs`, `period`, `chunk`, `next_break`, `simsec`, `seeds: list[int|None]`, `pause_at: sorted list[int]`, `stop_requested`, `started_at`, `run_started_at`, `run_wall_s: list[float]`, `error`, `warnings`.

**`start(runs, seed, seed_increment, chunk_sim_s, pause_at)` validatsiyasi:**
- Ulanish bor, boshqa job `running/paused` emas → aks holda `JOB_RUNNING`.
- `period = Simulation.SimPeriod` > 0.
- `runs > 1` va `seed is None` → joriy urug'ni `VISSIM_SEED_ATTR` dan o'qishga urinish; o'qib bo'lmasa `INVALID_ARGUMENT: seed required for runs>1`. **Sabab:** bir xil urug' bilan replikatsiyalar aynan bir xil natija beradi.
- Urug'lar oldindan hisoblanadi: `seed + (k-1)*seed_increment` va javobda qaytariladi.
- `pause_at` qiymatlari `(0, period)` oralig'ida bo'lishi kerak; bo'lak chegaralariga qo'shiladi.

**`tick()` — bitta qadam (worker oqimida):**
```
if stop_requested:            Simulation.Stop(); state = stopped; return
target = min(next_break, period, nearest pause_at > simsec)
if target < period:
    Simulation.SimBreakAt = target
    Simulation.RunContinuous()                 # target'da pauza qiladi
    new = Simulation.SimSec
    if new <= simsec + eps:  state = error("simulation did not advance")   # tashqi to'xtash yoki tashlab ketilgan break
    simsec = new
    if target in pause_at:   state = paused
    next_break = target + chunk (agar target bo'lak chegarasi bo'lsa)
else:  # oxirgi bo'lak
    Simulation.SimBreakAt = VISSIM_BREAK_DISABLE_VALUE     # (*) eski break'ni o'chirish
    Simulation.RunContinuous()                              # run oxirigacha
    simsec = period; run_wall_s.append(...)
    if run < runs: run += 1; urug'ni qo'yish; simsec = 0; next_break = chunk
    else: state = finished
except COM xato: state = error(kod + xabar)
```

**Muhim nozikliklar (M6 da haqiqiy Vissim'da tekshiriladi):**
- `SimBreakAt` ni 0 qilish "to'xtash yo'q" degan ma'noni bildiradimi — tasdiqlanmagan; shuning uchun `VISSIM_BREAK_DISABLE_VALUE` sozlanadi. Muqobil: `SimBreakAt = period` yoki `period + 1`.
- Run tugagandan keyin `SimSec` nima qaytarishi (0 yoki period) — tasdiqlanmagan; kod buni ishlatmaydi.
- Ko'p run natijalari saqlanib qolishi Evaluation sozlamasiga bog'liq ("keep previous results" turidagi atribut, nomi tasdiqlanmagan). `start` javobiga `runs > 1` bo'lsa ogohlantirish qo'shiladi: "ensure evaluation keeps previous runs".
- Signal holati faqat pauza paytida o'zgartirilishi kerak; `pause_at` aynan shuning uchun.

**Holatni o'qish** (`status`, `health`) COM'ga murojaat qilmaydi — worker yozgan maydonlarni o'qiydi (GIL ostida atomar). Foiz: `((run-1)*period + simsec) / (runs*period) * 100`.

### 6.7 Fayl siyosati (`policy.py`)

- Barcha yo'llar `Path(p).expanduser().resolve(strict=False)`.
- Ruxsat: `any(path.is_relative_to(root) for root in allowed_roots)` — `startswith` emas (`D:\VissimProjects2` muammosi). Windows'da `PureWindowsPath` registrga befarq.
- Ildizlar ro'yxati bo'sh → `PATH_DENIED: no allowed directories configured` (fail-closed).
- Yozish (`save_network`, `export_csv`) → qo'shimcha: readonly ildizlar ichida bo'lmasligi shart → `READ_ONLY_PATH`.
- Kengaytma oq ro'yxati: yuklash `.inpx`/`.layx`; saqlash `.inpx`; eksport `.csv`.
- ASCII bo'lmagan yo'l → ishlaydi, lekin `warnings: ["non-ASCII path; if Vissim fails to read it, move the project to an ASCII path"]`.
- `save_network`: mavjud fayl ustiga yozishdan oldin `backups\<stem>_YYYYMMDD_HHMMSS<ext>` ga nusxa; yuklangan asl fayl ustiga yozish uchun `overwrite_loaded=True` aniq talab qilinadi.

**Atribut siyosati:**
- `set_attributes` / `bulk`: atribut nomida (registrga befarq) `File`, `Path`, `Folder`, `Dir` bo'lsa va `VISSIM_ALLOW_PATH_ATTRS=0` → `ATTRIBUTE_DENIED` (masalan `SupplyFile` orqali fayl siyosatini chetlab o'tishning oldini olish).

### 6.8 Audit (`audit.py`)

- Fayl: `VISSIM_AUDIT_DIR\audit-YYYY-MM.jsonl`, har yozuv bitta qator.
- Maydonlar: `ts` (ISO, mahalliy vaqt zonasi bilan), `tool`, `generation`, `pid`, `args` (yo'llar, atributlar), `old`, `new`, `dry_run`, `ok`, `error_code`, saqlashda fayl `sha256` va zaxira yo'li, `exec` da kodning to'liq matni.
- Audit yozilmasa (disk xatosi) — operatsiya bajariladi, lekin javobga `warnings` qo'shiladi va stderr'ga log.

### 6.9 Chiqish cheklovlari (`output.py`)

- `to_jsonable`: `None/bool/int/str` o'zgarmaydi; `float` NaN/Inf → `None` + `warnings`; `tuple/list` rekursiv; `datetime`/`pywintypes.Time` → ISO satr; qolgani `str()` (200 belgigacha qisqartirilgan).
- Qatorlar: `limit ≤ VISSIM_MAX_ROWS`; javobda doim `total`, `offset`, `returned`, `truncated`.
- Javob JSON hajmi `VISSIM_MAX_RESULT_CHARS` dan oshsa → qatorlar kamaytiriladi, `truncated: true`, `hint: "use vissim_export_csv"`.

### 6.10 Fake backend (`tests/fake_vissim.py`, `tests/fakes/`)

`FakeVissim` haqiqiy Vissim semantikasini kerakli darajada taqlid qiladi:
- `Simulation`: `AttValue/SetAttValue` uchun `SimPeriod`, `SimRes`, `SimBreakAt`, `SimSec`, `RandSeed`, `UseMaxSimSpeed`; `RunContinuous()` — `SimBreakAt > SimSec` bo'lsa o'sha joyda pauza, aks holda run oxirigacha (keyin yangi run boshlanadi); `RunSingleStep()`, `Stop()`; har bir run'ning urug'ini yozib boradi.
- `Net`: `Links`, `SignalControllers[*].SGs`, `VehicleInputs`, `DataCollectionMeasurements` — `Count`, `ItemByKey`, `GetMultipleAttributes`, `AttValue/SetAttValue`; ba'zi atributlar read-only (yozishda COM-ga o'xshash xato).
- `LoadNet`, `LoadLayout`, `SaveNetAs`, `Exit`, `SuspendUpdateGUI`, `Graphics.CurrentNetworkWindow`.
- Nosozlik in'ektsiyasi: `hang_next_call(seconds)`, `raise_com_error_next(hresult)`, `print_to_stdout_on_call`, `stale_break_at=…`, `crash()` (keyingi chaqiruvlar `RPC_E_DISCONNECTED`).
- `tests/fakes/` dagi soxta `pythoncom`, `pywintypes` (`com_error` sinfi bilan), `win32com/client/dynamic.py` — `PYTHONPATH` orqali ulanadi; `Dispatch()` `FakeVissim` qaytaradi.

---

## 7. Tool katalogi (v1.0)

Umumiy qoidalar:
- Nomlash: `vissim_<fe'l>_<obyekt>`; Claude Code'da `mcp__vissim__<tool>`.
- Har bir javob: `{"ok": true, ...}` yoki `{"ok": false, "error": {"code", "message", "hint"}}`; ixtiyoriy `"warnings": [...]`.
- Docstring inglizcha, birinchi qatorda nima qilishi, keyin parametrlar misoli. Tavsif 4 096 belgidan qisqa (yuqori darajadagi ma'lumot boshida).
- Agar o'rnatilgan `mcp` SDK `ToolAnnotations` ni qo'llasa: o'qish toollariga `readOnlyHint=True`, buzuvchilarga `destructiveHint=True`.

### 7.1 Hayot sikli

| Tool | Parametrlar | Qaytaradi | COM | Turi |
|---|---|---|---|---|
| `vissim_connect` | `progid?: str` | `pid`, `progid`, `job_object: bool`, `warnings` | `Dispatch` | yozuvchi (resurs band qiladi) |
| `vissim_health` | — | `connected`, `pid`, `generation`, `busy_seconds`, `current_task`, `job` (qisqa), `settings` (maxfiy bo'lmagan), `version` (server) | **yo'q** | o'qish |
| `vissim_info` | — | yuklangan fayl (server kuzatgan), asosiy kolleksiyalar `Count` (har biri `suppress` bilan), `SimPeriod`, `SimRes` | ha | o'qish |
| `vissim_restart` | — | yangi `pid`, `aborted_job_id?` | kill + `Dispatch` | buzuvchi |
| `vissim_close` | — | `closed`, `method: exit|terminate|kill` | `Exit` | buzuvchi |

### 7.2 Fayllar

| Tool | Parametrlar | Qaytaradi | Izoh |
|---|---|---|---|
| `vissim_load_network` | `inpx_path`, `layx_path?`, `additive=false` | yuklangan yo'l, kolleksiya sonlari, `warnings` | `LoadNet(path, additive)`, `LoadLayout`; timeout `VISSIM_LOAD_TIMEOUT_S`; ishlayotgan job bo'lsa `JOB_RUNNING` |
| `vissim_save_network` | `save_as`, `overwrite_loaded=false` | saqlangan yo'l, `backup_path?`, `sha256` | `SaveNetAs`; siyosat 6.7; audit |

### 7.3 Obyektlar

| Tool | Parametrlar | Qaytaradi | Izoh |
|---|---|---|---|
| `vissim_describe` | `path` | `members: [{name, kind: method|property_get|property_put}]` | COM type info (`_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)` → `GetNames(memid)`); type info bo'lmasa `INFO_UNAVAILABLE` |
| `vissim_list_objects` | `collection`, `attributes: list[str]`, `offset=0`, `limit=100` | `columns`, `rows`, `total`, `returned`, `truncated` | `GetMultipleAttributes(tuple)`; natija atributlari ham shu yerda (`"Vehs(Current,Last,All)"`) |
| `vissim_get_attributes` | `path`, `attributes` | `values: {attr: value}`, `errors: {attr: message}` | Har atribut alohida; bittasi xato bo'lsa qolganlari qaytadi |
| `vissim_set_attributes` | `path`, `values: dict`, `dry_run=false` | `old`, `new`, `dry_run`, `failed` | Avval hammasini o'qiydi, keyin yozadi, keyin qayta o'qiydi; audit |
| `vissim_set_attribute_bulk` | `collection`, `attribute`, `items: [{key, value}]`, `dry_run=false` | `updated`, `failed: [{key, error}]` | Bitta COM vazifasi ichida `ItemByKey` sikli; ≤ 5000 element; audit |

### 7.4 Simulyatsiya

| Tool | Parametrlar | Qaytaradi | Izoh |
|---|---|---|---|
| `vissim_configure_simulation` | `period_s?`, `resolution?`, `seed?`, `max_speed=true`, `quick_mode=true`, `suspend_gui=true`, `extra: dict?` | joriy `SimPeriod`, `SimRes`, urug' | `UseMaxSimSpeed`, `QuickMode`, `SuspendUpdateGUI`; `extra` — Simulation'ga ixtiyoriy atributlar (`NumCores` kabi tasdiqlanmaganlar shu orqali) |
| `vissim_start_simulation` | `runs=1`, `seed?`, `seed_increment=1`, `chunk_sim_s?`, `pause_at: list[int]?` | `job_id`, `period`, `runs`, `seeds`, `warnings` | **Darhol** qaytadi; 6.6 |
| `vissim_simulation_status` | `job_id?` | `state`, `run/runs`, `simsec`, `percent`, `elapsed_s`, `eta_s?`, `seeds`, `error` | COM'siz |
| `vissim_wait_simulation` | `job_id?`, `max_wait_s=60` (≤ `VISSIM_WAIT_MAX_S`) | `status` bilan bir xil + `waited_s` | `asyncio.sleep(1)` bilan polling; job `running` holatdan chiqsa yoki muddat tugasa qaytadi. Claude'ning chaqiruvlar sonini kamaytiradi |
| `vissim_resume_simulation` | `job_id?` | holat | `paused` → `running` |
| `vissim_stop_simulation` | `job_id?` | `stopping` | Keyingi bo'lak chegarasida to'xtaydi |

### 7.5 Natijalar va signallar

| Tool | Parametrlar | Qaytaradi | Izoh |
|---|---|---|---|
| `vissim_export_csv` | `collection`, `attributes`, `csv_path` | `path`, `rows` | `;` ajratuvchi, `utf-8-sig` (Excel uchun); siyosat 6.7; audit |
| `vissim_set_signal_state` | `sc_no`, `sg_no`, `state: str|int?`, `release_to_controller=false` | `SigState`, `ContrByCOM` | Job `running` bo'lsa `JOB_RUNNING` (faqat `paused` yoki job yo'q holatda); `SigState` yozilganda `ContrByCOM` avtomatik yoqiladi; `release=true` — `ContrByCOM=False`; audit |

### 7.6 Diagnostika

| Tool | Parametrlar | Qaytaradi | Izoh |
|---|---|---|---|
| `vissim_tail_log` | `lines=100` (≤ 500) | log oxiri | Faqat server log fayli; yo'l parametri yo'q |
| `vissim_exec_python` | `code` | `result` | `VISSIM_MCP_ALLOW_EXEC=1` bo'lmasa `EXEC_DISABLED`; nom fazosida faqat `Vissim` va `result`; kod to'liq audit qilinadi |

### 7.7 Xato kodlari (`errors.py`)

`NOT_CONNECTED`, `ALREADY_CONNECTED`, `INVALID_ARGUMENT`, `PATH_DENIED`, `READ_ONLY_PATH`, `FILE_NOT_FOUND`, `ATTRIBUTE_DENIED`, `RESOLVE_FAILED`, `COM_ERROR`, `TIMEOUT_HUNG`, `JOB_RUNNING`, `JOB_NOT_FOUND`, `EXEC_DISABLED`, `INFO_UNAVAILABLE`, `SHUTTING_DOWN`, `INTERNAL`.

Har birida inglizcha `message` va mavjud bo'lsa amaliy `hint`.

---

## 8. Xavfsizlik va korporativ masalalar

1. **Litsenziya:** bitta nusxa; `KILL_ON_EXIT` va Job Object floating litsenziyada seat band bo'lib qolishining oldini oladi. Parallel nusxalar — v1.0 da yo'q.
2. **Fayl tizimi:** fail-closed ildizlar, readonly asl modellar, kengaytma oq ro'yxati, yo'l atributlarini yozish taqiqi.
3. **Kod bajarish:** `exec` standart holatda o'chiq; yoqilganda ham faqat test kompyuterida, to'liq audit bilan.
4. **Claude Code ruxsatlari:** o'qish toollari `allow`, qolganlari har safar tasdiqlanadi (`examples/settings.json.example`, 9.2).
5. **Maxfiylik:** javoblarda faqat so'ralgan ustunlar; katta jadvallar CSV'da; Q7 hal bo'lmaguncha faqat test modellari.
6. **Log'lar:** maxfiy ma'lumot (token, parol) log'ga yozilmaydi; atribut qiymatlari faqat audit faylida.
7. **Antivirus/EDR:** `python.exe → DCOM → Vissim.exe` zanjiri uchun IT'ga venv va Vissim yo'llari oldindan beriladi.
8. **Administrator huquqi** faqat bir martalik amallarga (COM register, pywin32 post-install); server oddiy foydalanuvchi bilan ishlaydi. Vissim'ni administrator sifatida ochilgan holda qoldirmang — Job Object biriktirilmaydi.

---

## 9. Claude Code integratsiyasi

### 9.1 `.mcp.json` (Vissim loyihasi papkasida)

```json
{
  "mcpServers": {
    "vissim": {
      "type": "stdio",
      "command": "C:\\vissim-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-X", "utf8", "-m", "vissim_mcp"],
      "env": {
        "PYTHONUTF8": "1",
        "VISSIM_PROGID": "Vissim.Vissim",
        "VISSIM_ALLOWED_DIRS": "D:\\VissimProjects",
        "VISSIM_KILL_ON_EXIT": "1"
      },
      "timeout": 900000
    }
  }
}
```

Yoki CLI orqali (`--env` va server nomi orasida boshqa parametr bo'lishi shart):

```powershell
claude mcp add --scope project --env PYTHONUTF8=1 --env VISSIM_ALLOWED_DIRS=D:\VissimProjects `
  --transport stdio vissim -- C:\vissim-mcp\.venv\Scripts\python.exe -X utf8 -m vissim_mcp
```

> Desktop ilovasining Code yorlig'ida bir xil nomli server ham `~/.claude.json` (user scope), ham `.mcp.json` da bo'lsa, `~/.claude.json` dagisi ishlatiladi — nomni ikki joyda takrorlamang.

### 9.2 `.claude/settings.json` — ruxsatlar

```json
{
  "permissions": {
    "allow": [
      "mcp__vissim__vissim_health",
      "mcp__vissim__vissim_info",
      "mcp__vissim__vissim_describe",
      "mcp__vissim__vissim_list_objects",
      "mcp__vissim__vissim_get_attributes",
      "mcp__vissim__vissim_simulation_status",
      "mcp__vissim__vissim_wait_simulation",
      "mcp__vissim__vissim_tail_log"
    ],
    "deny": [
      "mcp__vissim__vissim_exec_python"
    ]
  }
}
```

### 9.3 `CLAUDE.md` (Vissim loyihasi uchun, `examples/CLAUDE.md.example`)

```markdown
# Vissim loyihasi qoidalari
- Vissim bilan faqat `vissim` MCP toollari orqali ishla; Python COM skriptini o'zing yozib ishga tushirma.
- Ishni `vissim_health` bilan boshla; ulanmagan bo'lsa `vissim_connect`.
- Asl modellar `models\original\` da — faqat o'qiladi. Ishni nusxa ustida qil va `vissim_save_network` bilan yangi nomga saqla.
- Har qanday yozishdan oldin `dry_run=true` bilan eski/yangi qiymatlarni ko'rsat va tasdiq ol.
- Atribut nomini taxmin qilma: `vissim_describe` yoki `vissim_get_attributes` bilan tekshir; noaniq bo'lsa COM Help'dagi nomni so'ra.
- Simulyatsiya: `vissim_configure_simulation` → `vissim_start_simulation` → `vissim_wait_simulation` (takror) → natijalar.
- Bir nechta run uchun har doim `seed` ber.
- 200 qatordan ko'p natija — `vissim_export_csv`.
- Signal holatini faqat `pause_at` bilan pauza qilingan simulyatsiyada o'zgartir; oxirida `release_to_controller=true`.
- Ish oxirida `vissim_close` (litsenziya seat'ini bo'shatish).
```

---

## 10. Test strategiyasi

### 10.1 Unit (haqiqiy Vissim'siz, `pytest tests/unit`)

| Modul | Majburiy testlar |
|---|---|
| `config` | standartlar; noto'g'ri qiymatlar (manfiy timeout, bo'sh regex) → ishga tushishda aniq xato |
| `policy` | `is_relative_to` (`D:\A` vs `D:\A2`); bo'sh ildiz → rad; readonly; kengaytma; ASCII ogohlantirishi; yo'l atributi taqiqi |
| `resolver.parse_path` | oddiy, kalitli, satr kalitli, `Vissim.` prefiksi, noto'g'ri sintaksis, `_` bilan boshlanuvchi segment rad |
| `output` | NaN/Inf, chuqur ichma-ich ro'yxat, qisqartirish, `MAX_RESULT_CHARS` |
| `errors` | `com_error` → kod/hint xaritasi |
| `jobs` (FakeVissim) | 1 run; 3 run va urug'lar farqli; `seed=None, runs>1` → urug' o'qiladi yoki rad; **eski `SimBreakAt` qolgan holat** to'g'ri tugaydi; `chunk ≥ period`; `pause_at` → `paused` → `resume`; `stop`; COM xato → `error`; "did not advance" |
| `worker/service` | timeout → `TIMEOUT_HUNG`; timeout'dan keyin `health.busy_seconds > 0`; restart → generation++, eski job `aborted`; navbat bo'lak orasida bo'shatiladi |
| `winproc` | PID diff mantig'i (psutil mock); 0 / 1 / 2 yangi PID; PID noma'lumda kill chaqirilmaydi |
| `audit` | yozuv formati; disk xatosida operatsiya davom etadi |
| tools | har bir tool uchun "baxtli yo'l" + kamida bitta xato yo'li |

### 10.2 Protokol (`tests/protocol`)

- Serverni `python -m vissim_mcp` sifatida subprocess'da ishga tushirish (`PYTHONPATH=tests/fakes`).
- `initialize` → `notifications/initialized` → `tools/list` → bir nechta `tools/call`.
- **stdout'dagi har bir qator JSON-RPC** ekanini tekshirish — shu jumladan fake ataylab `print()` va `os.write(1, ...)` qiladigan holatda.
- stdin yopilganda jarayon ≤ 300 ms ichida chiqishi.
- Kirill/o'zbekcha belgili yo'l (`Samarqand_ko'cha`) bilan yuklash.

### 10.3 Haqiqiy Vissim (`tests/real`, `@pytest.mark.vissim`, Windows'da qo'lda)

1. **Smoke:** `Examples Training\COM\Basic Commands` tarmog'i: connect → load → `Links.Count` → 60 s simulyatsiya → close. Jarayon qolmagan bo'lishi kerak.
2. **Nomlarni tasdiqlash:** `scripts/probe_com_names.py` — `docs/com-names.md` dagi har bir (*) nomni `AttValue` bilan o'qiydi, iloji bo'lsa yozib-qaytaradi, natijani jadvalga yozadi.
3. **Break semantikasi:** `SimBreakAt=VISSIM_BREAK_DISABLE_VALUE` bilan run oxirigacha boradimi; eski break qolgan holat.
4. **Stress:** 3600 s × 5 run; `percent` monoton o'sadi; `stop` bir bo'lak ichida ishlaydi; Claude sessiyasi bloklanmaydi.
5. **Nosozlik:** Vissim'ni Task Manager'dan o'ldirish → tushunarli xato, `restart` tiklaydi. "Save changes?" dialogini qo'lda ochish → `TIMEOUT_HUNG`. Claude Code'ni yopish → Vissim jarayoni yopiladi (Job Object).
6. **Kodirovka:** ASCII bo'lmagan yo'ldagi tarmoq.

### 10.4 Qabul mezonlari (v1.0)

- Barcha unit va protokol testlari yashil (Windows va Linux'da).
- 10.3 dagi 1–6 qo'lda o'tkazilgan, natijalar `docs/test-report-v1.md` da.
- `docs/com-names.md` dagi har bir nom "tasdiqlandi / rad etildi / sozlanadi" holatida.
- Claude Code'da `/mcp` → `vissim ✔`, toollar soni 22 (7-bo'lim katalogi).
- Claude Code sessiyasi yopilgandan keyin `Vissim*.exe` jarayoni qolmaydi.

---

## 11. Bosqichlar (milestones)

Har bosqich — bitta Claude Code sessiyasi, alohida git branch va commit'lar. Bosqich DoD bajarilmaguncha keyingisi boshlanmaydi.

| # | Bosqich | Natija | Tayyorlik mezoni (DoD) |
|---|---|---|---|
| **M0** | Pre-flight (Vissim kompyuterida) | `scripts/preflight.ps1`, `docs/com-names.md` (boshlang'ich jadval), `docs/preflight-report.md` | Litsenziyada COM bor; COM ro'yxatdan o'tgan; `HKEY_CLASSES_ROOT\Vissim.Vissim*` ProgID'lari ro'yxati; 64-bit Python; minimal `Dispatch → Net.Links.Count → Exit` ishladi; Q1–Q7 javoblari yozilgan |
| **M1** | Skelet | `pyproject`, `config`, `errors`, `logging_setup`, `stdio_guard`, `output`, `__main__`, bo'sh `server` (faqat `vissim_health`), fakes, CI | `pytest` yashil; protokol testi: `tools/list` ishlaydi, stdout toza (ataylab print bilan ham); `ruff` toza |
| **M2** | Worker va hayot sikli | `backend/*`, `worker`, `service`, `winproc`, `tools/lifecycle` | 10.1 dagi worker/service/winproc testlari; fake bilan connect/health/restart/close; stdin EOF'da ≤ 300 ms chiqish |
| **M3** | Fayllar va obyektlar | `policy`, `audit`, `resolver`, `tools/files`, `tools/objects` | policy/resolver testlari; dry_run; bulk; audit yozuvlari; describe fake'da |
| **M4** | Simulyatsiya | `jobs`, `tools/simulation`, `tools/results` | 10.1 `jobs` testlari to'liq (eski break holati ham); `wait` ≤ `VISSIM_WAIT_MAX_S`; CSV eksport |
| **M5** | Signallar va diagnostika | `tools/signals`, `tools/diagnostics` | signal faqat paused'da; exec standart o'chiq; tail_log |
| **M6** | Haqiqiy Vissim'da tekshirish | `scripts/probe_com_names.py`, `tests/real/*`, `docs/test-report-v1.md` | 10.3 hammasi o'tkazilgan; topilgan farqlar kodda/konfiguratsiyada tuzatilgan; `com-names.md` yakunlangan |
| **M7** | Integratsiya va reliz | `README.md`, `examples/*`, `docs/troubleshooting.md`, `v1.0.0` tegi | 10.4 qabul mezonlari; README bo'yicha toza kompyuterga o'rnatish 30 daqiqada bajariladi |

---

## 12. Xavflar

| Xavf | Ehtimol | Ta'sir | Choralar |
|---|---|---|---|
| Litsenziyada COM yo'q | o'rta | to'xtatuvchi | M0 birinchi qadam; yo'q bo'lsa distribyutordan modul so'rash |
| Atribut nomlari versiyaga qarab farq qiladi | yuqori | o'rta | qattiq kodlanmagan; `probe_com_names.py`; umumiy get/set |
| `SimBreakAt` semantikasi kutilganidan farq qiladi | o'rta | yuqori (noto'g'ri "finished") | sozlanadigan qiymat; "did not advance" tekshiruvi; M6 maxsus testi |
| Modal dialog COM'ni bloklaydi | yuqori | o'rta | watchdog, `TIMEOUT_HUNG` hint, `restart`, litsenziya default |
| Vissim zombie bo'lib qoladi, seat band | o'rta | o'rta | Job Object + kill-on-exit + aniq PID qoidasi |
| Job Object'ga biriktirish rad etiladi | past | o'rta | ogohlantirish + kill-on-exit zaxirasi; Vissim'ni admin bilan ochmaslik |
| `mcp` SDK API o'zgaradi | o'rta | past | versiya chegarasi; M1 da API tekshiruvi; protokol testi |
| Claude Code timeout xatti-harakati o'zgaradi | o'rta | past | hech qaysi tool uzoq kutmaydi; job dizayni |
| EDR jarayon zanjirini bloklaydi | o'rta | yuqori | IT bilan oldindan kelishish |
| Ma'lumotlar tashqi API'ga chiqishi | — | siyosat | Q7; minimal ustunlar; test modellari |

---

## 13. Claude Code uchun promptlar

Har bir bosqich boshida Claude Code'ga quyidagini bering (repozitoriya ildizida `PLAN.md` turgan bo'lsin).

### Umumiy kirish (har sessiyaning boshiga)

```text
Repozitoriyadagi PLAN.md — bu loyihaning yagona manbai. Uni to'liq o'qi.
Qoidalar:
- Faqat joriy bosqich (M?) doirasida ishlash; keyingi bosqichlarga o'tma.
- PLAN.md dan chetga chiqish kerak bo'lsa — avval sababini ayt va mendan tasdiq ol.
- Kod, identifikatorlar, docstring, log, commit — inglizcha; menga javoblar — o'zbekcha.
- Tasdiqlanmagan Vissim COM nomlarini (PLAN.md 5-bo'lim, (*) belgilari, docs/com-names.md) qattiq kodlama.
- Har o'zgarishdan keyin testlarni ishga tushir; DoD bajarilganini isbotlovchi buyruq chiqishini ko'rsat.
- Bosqich oxirida: nima qilindi, DoD bo'yicha holat, ochiq qolgan savollar.
```

### M0

```text
Bosqich M0 (Pre-flight). Bu Windows kompyuterida PTV Vissim o'rnatilgan.
1) scripts/preflight.ps1 yoz: Python 64-bit tekshiruvi; HKCR\Vissim.Vissim* ProgID'larini ro'yxatlash;
   Vissim*.exe jarayonlari; CodeMeter xizmati holati. Natijani docs/preflight-report.md ga yoz.
2) Alohida venv yarat va pywin32 o'rnat; minimal tekshiruv: dynamic.Dispatch(ProgID) → Net.Links.Count → Exit.
   Chiqishni stderr'ga yoz.
3) docs/com-names.md jadvalini PLAN.md 5- va 6.6-bo'limlaridagi (*) nomlar hamda hujjatdagi
   "tasdiqlanmagan" ro'yxati asosida tuz: nom | qayerda ishlatiladi | holat (unverified).
4) PLAN.md 2-bo'limdagi Q1–Q7 savollarini menga ber va javoblarni preflight-report.md ga yoz.
Litsenziya oynasini o'zim tekshiraman — mendan so'ra.
```

### M1

```text
Bosqich M1 (Skelet). PLAN.md 4.2 dagi tuzilmani yarat; 5-bo'lim (config), 6.1 (stdio_guard),
6.9 (output), 7.7 (errors) ni to'liq amalga oshir. Server faqat vissim_health tooli bilan ishga tushsin.
tests/fakes va tests/fake_vissim.py ning boshlang'ich versiyasini yoz (6.10).
O'rnatilgan mcp SDK'da stdio_server(stdout=...) parametri borligini tekshir va natijani README'ga yoz.
Protokol testi: initialize → tools/list; stdout'dagi har qator JSON ekanini, fake print() va os.write(1)
qilgan holatda ham tekshir. DoD: PLAN.md 11-bo'lim M1.
```

### M2

```text
Bosqich M2 (Worker va hayot sikli). PLAN.md 4.3, 6.2, 6.3, 6.4 va 7.1 ni amalga oshir:
backend/base.py + backend/real.py, worker.py, service.py, winproc.py (Job Object ctypes bilan,
Windows'dan tashqarida no-op), tools/lifecycle.py. Yopilish ketma-ketligi 6.4 dagi 200 ms byudjetga mos bo'lsin.
PID aniq bo'lmasa hech qanday jarayon o'ldirilmasin. 10.1 dagi worker/service/winproc testlarini yoz.
DoD: PLAN.md 11-bo'lim M2.
```

### M3

```text
Bosqich M3 (Fayllar va obyektlar). PLAN.md 6.5, 6.7, 6.8, 7.2, 7.3 ni amalga oshir.
Yo'l tekshiruvi is_relative_to bilan, fail-closed; readonly ildizlar; yo'l atributlari taqiqi; audit JSONL.
vissim_describe COM type info orqali, type info bo'lmasa INFO_UNAVAILABLE.
10.1 dagi policy/resolver/audit/tools testlari. DoD: PLAN.md 11-bo'lim M3.
```

### M4

```text
Bosqich M4 (Simulyatsiya). PLAN.md 6.6 dagi holat mashinasini aynan shu ko'rinishda amalga oshir
(pause_at, stop, ko'p run va urug'lar, eski SimBreakAt holati, "did not advance" tekshiruvi),
7.4 dagi toollar va 7.5 dagi vissim_export_csv. vissim_wait_simulation hech qachon VISSIM_WAIT_MAX_S dan
uzoq kutmasin. FakeVissim'ga RunContinuous/SimBreakAt semantikasi va nosozlik in'ektsiyalarini qo'sh.
10.1 jobs testlari to'liq. DoD: PLAN.md 11-bo'lim M4.
```

### M5

```text
Bosqich M5 (Signallar va diagnostika). PLAN.md 7.5 (vissim_set_signal_state) va 7.6
(vissim_tail_log, vissim_exec_python — standart o'chiq) ni amalga oshir. Signal holati job running bo'lsa
rad etilsin. Testlar va audit. DoD: PLAN.md 11-bo'lim M5.
```

### M6

```text
Bosqich M6 (Haqiqiy Vissim). scripts/probe_com_names.py yoz: docs/com-names.md dagi har bir nomni
haqiqiy Vissim'da sinab, jadvalni yangilasin. Keyin PLAN.md 10.3 dagi 1–6 testlarni men bilan birga o'tkaz
(Task Manager, dialog kabi qo'lda qadamlarni menga ayt). Topilgan farqlarni kod yoki konfiguratsiyada tuzat,
natijani docs/test-report-v1.md ga yoz. DoD: PLAN.md 11-bo'lim M6.
```

### M7

```text
Bosqich M7 (Integratsiya va reliz). README.md (o'zbekcha: o'rnatish, sozlash, ishlatish, muammolar),
examples/.mcp.json.example, examples/settings.json.example, examples/CLAUDE.md.example (PLAN.md 9-bo'lim),
docs/troubleshooting.md. 10.4 qabul mezonlarini tekshir va v1.0.0 tegini qo'y.
```

---

## 14. Keyingi versiyalar uchun g'oyalar (v1.1+)

- `vissim_compare_scenarios(scenarios.csv)` — ssenariylarni server ichida ketma-ket bajarib, bitta jadval qaytarish (COM chaqiruvlari va Claude qadamlari kamayadi).
- Server ichidagi adaptiv signal strategiyalari (Claude faqat parametr beradi).
- `.att` / `.mer` natija fayllarini o'qish va qisqa xulosa.
- Mavjud Vissim oynasiga ulanish (ROT orqali, agar Vissim qo'llasa — tekshirilmagan).
- MCP progress bildirishnomalari (`wait` paytida).
