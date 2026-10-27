# vissim-mcp

PTV Vissim'ni COM interfeysi orqali boshqarish uchun **stdio MCP server** (Python). Claude Code (Windows'dagi
Desktop ilovasi yoki CLI) shu server orqali Vissim'ni ishga tushiradi, tarmoq yuklaydi, atributlarni o'qiydi/yozadi,
simulyatsiyani fon job sifatida yurgizadi, natijalarni CSV'ga chiqaradi va signal holatini boshqaradi.
Loyihaning yagona manbai — [`PLAN.md`](PLAN.md); bosqichlararo kelishuvlar —
[`docs/implementation-contract.md`](docs/implementation-contract.md).

## Holat

| Bosqich | Holat |
|---|---|
| M0 — Pre-flight | bajarildi ([`docs/preflight-report.md`](docs/preflight-report.md), [`docs/com-names.md`](docs/com-names.md)) |
| M1 — Skelet | bajarildi (konfiguratsiya, xato kodlari, log, stdout himoyasi, chiqish cheklovlari, soxta COM qatlami) |
| M2 — Worker va hayot sikli | bajarildi (bitta STA oqim, watchdog, PID/Job Object, 5 ta hayot sikli tooli) |
| M3 — Fayllar va obyektlar | bajarildi (yo'l siyosati, audit, rezolver, 7 ta tool) |
| M4 — Simulyatsiya | bajarildi (job holat mashinasi, 6 ta simulyatsiya tooli, CSV eksport) |
| M5 — Signallar va diagnostika | bajarildi (`vissim_set_signal_state`, `vissim_tail_log`, `vissim_exec_python`) |
| **M6 — Haqiqiy Vissim'da tekshirish** | **to'xtatilgan**: kompyuterdagi yagona litsenziya — Demo (COM moduli yo'q). Hamma narsa tayyor: [`docs/m6-runbook.md`](docs/m6-runbook.md) |
| M7 — Integratsiya va reliz | bajarildi: o'rnatuvchi, doctor, ro'yxatdan o'tkazish yordamchisi, misollar, hujjatlar. `v1.0.0` tegi M6 dan keyin qo'yiladi |

Server versiyasi hozir `1.0.0rc1` (`src/vissim_mcp/__init__.py`).

## Talablar

| Talab | Izoh |
|---|---|
| Windows 10/11, interaktiv foydalanuvchi sessiyasi | Server Windows xizmati sifatida ishlamaydi |
| PTV Vissim 2024+ (64-bit), **litsenziyada COM moduli** | Demo/Student litsenziyada COM **yo'q** — Vissim COM orqali ochilganda "License not found" oynasi chiqadi va `vissim_connect` `CO_E_SERVER_EXEC_FAILURE` bilan tugaydi. Bu kompyuterda hozir faqat Demo bor (Q2) |
| Vissim COM server ro'yxatdan o'tgan | Vissim'ni administrator sifatida oching → Help → Register COM Server (bir marta) |
| Litsenziya "default" qilib saqlangan | Vissim oddiy ochilganda litsenziya tanlash dialogi chiqmasligi kerak |
| Python 3.12 (yoki 3.13 / 3.11), **64-bit**, `py` launcher bilan | python.org o'rnatuvchisi; o'rnatuvchi o'zi topadi |
| Claude Code — native Windows (WSL emas) | `%USERPROFILE%\.local\bin\claude.exe`; `claude --version` |
| Birinchi o'rnatishda internet | `mcp`, `pywin32`, `psutil` yuklab olinadi; keyingi yangilashlar pip keshidan |

## O'rnatish

PowerShell'da (administrator kerak emas):

```powershell
cd D:\PTV-Vissim
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

Taxminan 1–3 daqiqa (birinchi marta). O'rnatuvchi **idempotent** — `git pull` dan keyin yoki `doctor.ps1` muammo
ko'rsatganda qayta ishga tushiring. U nima qiladi:

1. `py` launcher orqali 64-bit Python 3.12 ni (bo'lmasa 3.13, 3.11) tanlaydi.
2. Repozitoriyadan **tashqarida** alohida runtime venv yaratadi yoki yangilaydi:
   `%LOCALAPPDATA%\vissim-mcp\venv`. Venv buzilgan bo'lsa (masalan, asosiy Python o'chirilgan yoki boshqa minor
   versiyaga yangilangan) — qayta yaratadi.
3. Paketni repozitoriyaning vaqtinchalik nusxasidan **editable bo'lmagan** holda o'rnatadi va paketning o'zini
   majburan qayta o'rnatadi (versiya o'zgarmagan bo'lsa ham joriy kod o'rnatiladi; repozitoriyada `build/` yoki
   `*.egg-info` qolmaydi).
4. Runtime'ni tekshiradi: 64-bit, bog'liqliklar import qilinadi, o'rnatilgan kod repozitoriya bilan aynan bir xil.
5. `<ProjectsDir>\models\original` papkasini yaratadi (asl modellar uchun, server u yerga yozmaydi).
6. Ro'yxatdan o'tkaziladigan **aynan o'sha muhit** bilan `python -X utf8 -m vissim_mcp --check` ni bajaradi.
7. MCP stdio smoke test (`scripts\mcp_smoke.py`): `initialize` → `tools/list` (22 ta tool) → `vissim_health`.
   Vissim ishga tushirilmaydi.
8. Claude Code'ga ro'yxatdan o'tkazadi (`scripts\register_claude.py` → `claude mcp add-json vissim <json> -s user`)
   va `claude mcp get vissim` bilan tekshiradi.

Oxirida natijalar jadvali chiqadi; xato bo'lsa — qaysi qadam va qanday tuzatish kerakligi, chiqish kodi `1`.

Parametrlar:

| Parametr | Standart | Ma'nosi |
|---|---|---|
| `-ProjectsDir` | `D:\VissimProjects` | `VISSIM_ALLOWED_DIRS` — server fayl o'qishi/yozishi mumkin bo'lgan yagona ildiz |
| `-ProgId` | `Vissim.Vissim` | `VISSIM_PROGID` (bir nechta Vissim versiyasi bo'lsa, masalan `Vissim.Vissim.2026`) |
| `-Scope` | `user` | `user` / `project` / `local` (pastga qarang; `user` tavsiya etiladi) |
| `-RuntimeDir` | `%LOCALAPPDATA%\vissim-mcp\venv` | Runtime venv (repozitoriya ichida bo'lishi taqiqlangan) |
| `-Python` | — | Asosiy Python'ni aniq ko'rsatish (`py` launcher o'rniga) |
| `-ExtraEnv` | — | Qo'shimcha o'zgaruvchilar: `-ExtraEnv 'VISSIM_CALL_TIMEOUT_S=180','VISSIM_MAX_ROWS=1000'` |
| `-NoRegister` | — | Claude Code'ga ro'yxatdan o'tkazmaslik (faqat o'rnatish va tekshirish) |
| `-Force` | — | Runtime venv'ni noldan qayta yaratish |
| `-Claude` | — | `claude.exe` yo'li (standart: PATH, keyin `%USERPROFILE%\.local\bin\claude.exe`) |

O'rnatishdan keyin **yangi** Claude Code sessiyasini oching (yoki ochiq sessiyada `/mcp` → `vissim` → Reconnect) va
`/mcp` da `vissim` ulangan (✔) ekanini, toollar soni 22 ekanini tekshiring.

> Muhim: Claude Code ishlab turgan server jarayonini yangilamaydi — stdio serverlar avtomatik qayta ulanmaydi.
> Qayta o'rnatishdan keyin har bir ochiq sessiyada `/mcp` → Reconnect qiling. Venv'ni qayta yaratish (`-Force`)
> uchun avval serverdan foydalanayotgan sessiyalarni yoping (aks holda `python.exe` fayli band bo'ladi).

### Nega user scope va qayta yoqilgandan keyin nima bo'ladi

- Server — oddiy stdio jarayon. Uni **Claude Code o'zi** har sessiya boshida ro'yxatdagi buyruq bo'yicha ishga
  tushiradi va sessiya tugaganda yopadi (stdin yopiladi, 300 ms dan keyin jarayon daraxti o'ldiriladi; server
  ≤ 300 ms ichida chiqadi va o'zi ochgan Vissim'ni yopadi). Windows xizmati, avtoyuklash yoki qo'lda ishga tushirish
  **kerak emas** — kompyuter qayta yoqilgandan keyin Claude Code ochilishi bilan server ishlaydi.
- `user` scope yozuvi `~/.claude.json` da saqlanadi: istalgan papkada ishlaydi, loyiha tasdiqlash so'rovi chiqmaydi,
  qayta yoqishdan keyin ham saqlanadi. Desktop ilovasining Code yorlig'i ham aynan `~/.claude.json` ni afzal ko'radi.
- Ro'yxatdagi buyruq — runtime venv'ning `python.exe` sining **absolyut yo'li**, argumentlar
  `-X utf8 -m vissim_mcp`, muhit `PYTHONUTF8=1`, `VISSIM_PROGID`, `VISSIM_ALLOWED_DIRS`, `VISSIM_KILL_ON_EXIT=1`,
  `timeout` 900 000 ms. PATH, joriy papka, faollashtirilgan venv yoki repozitoriyadagi `git checkout` ga bog'liq
  emas.
- Bitta nomni ikki scope'da ro'yxatdan o'tkazmang (ustuvorlik `local` > `project` > `user`). `register_claude.py`
  `vissim` boshqa scope'da topilsa, uni o'zgartirmaydi va xato bilan to'xtaydi (tuzatish buyrug'ini aytadi).
- Qayta yoqilgandan keyin server ishlamasa, sababi deyarli har doim tashqarida: CodeMeter xizmati ishga tushmagan,
  asosiy Python o'chirilgan/yangilangan, antivirus. `scripts\doctor.ps1` hammasini bir yo'la tekshiradi.

`project` scope (jamoaviy `.mcp.json`) kerak bo'lsa: `install.ps1 -Scope project` (`.mcp.json` `-ProjectsDir`
papkasida yaratiladi, birinchi ochilganda Claude Code tasdiq so'raydi) yoki
[`examples/.mcp.json.example`](examples/.mcp.json.example) ni loyiha papkasiga `.mcp.json` nomi bilan ko'chiring.
Bu holda user scope yozuvini olib tashlang (`scripts\uninstall.ps1`).

## Konfiguratsiya

Barcha `VISSIM_*` o'zgaruvchilar server ishga tushganda bir marta o'qiladi va tekshiriladi (`config.py`); noto'g'ri
qiymat — aniq xabar bilan chiqish kodi `2`. Joriy qiymatlarni `vissim_health` (`settings`) va
`python -X utf8 -m vissim_mcp --check` ko'rsatadi.

| O'zgaruvchi | Standart | Ma'nosi |
|---|---|---|
| `VISSIM_PROGID` | `Vissim.Vissim` | COM ProgID yoki `{CLSID}` (≤ 39 belgi) |
| `VISSIM_ALLOWED_DIRS` | bo'sh → `CLAUDE_PROJECT_DIR` | `;` bilan ajratilgan ruxsat etilgan ildizlar. Ikkalasi ham bo'sh bo'lsa **barcha fayl operatsiyalari rad etiladi**. O'rnatuvchi `-ProjectsDir` ni yozadi |
| `VISSIM_READONLY_DIRS` | har bir ildiz uchun `<ildiz>\models\original` | Bu yerlarga yozish/saqlash taqiqlangan |
| `VISSIM_CALL_TIMEOUT_S` | `120` | Oddiy COM vazifasi watchdog'i (s) |
| `VISSIM_CONNECT_TIMEOUT_S` | `180` | Vissim ishga tushishi (s) |
| `VISSIM_LOAD_TIMEOUT_S` | `600` | Tarmoq yuklash/saqlash (s) |
| `VISSIM_CHUNK_SIM_S` | `60` | Bir bo'lakdagi simulyatsiya soniyalari (1…2 678 400) |
| `VISSIM_WAIT_MAX_S` | `90` | `vissim_wait_simulation` chegarasi (< 120) |
| `VISSIM_EXE_PATTERN` | `^vissim\d*\.exe$` | Vissim jarayon nomi regex'i (registrga befarq, bo'sh — xato; tizim jarayonlariga mos naqsh rad etiladi) |
| `VISSIM_PID_DETECT_S` | `20` | Yangi PID'ni kutish (s) |
| `VISSIM_KILL_ON_EXIT` | `1` | Server tugaganda o'z Vissim'ini majburan yopish (Q5) |
| `VISSIM_USE_JOB_OBJECT` | `1` | Vissim'ni kill-on-close Job Object'ga biriktirish (faqat `KILL_ON_EXIT=1` bilan) |
| `VISSIM_MAX_ROWS` | `500` | Bitta javobdagi maksimal qator |
| `VISSIM_MAX_RESULT_CHARS` | `40000` | Javob JSON hajmi (≥ 1000; 50 000 dan oshsa Claude Code natijani faylga saqlaydi — ogohlantirish) |
| `VISSIM_ALLOW_PATH_ATTRS` | `0` | Nomida File/Path/Folder/Dir bo'lgan atributlarni yozishga ruxsat |
| `VISSIM_MCP_ALLOW_EXEC` | `0` | `vissim_exec_python` ni yoqish (faqat test kompyuterida) |
| `VISSIM_LOG_DIR` | `%LOCALAPPDATA%\vissim-mcp\logs` | Server log |
| `VISSIM_AUDIT_DIR` | `%LOCALAPPDATA%\vissim-mcp\audit` | Audit JSONL |
| `VISSIM_SEED_ATTR` | `RandSeed` (*) | Tasodifiy urug' atributi |
| `VISSIM_BREAK_DISABLE_VALUE` | `0` (*) | "To'xtash nuqtasi yo'q" qiymati (≥ 0) |

(*) — haqiqiy Vissim'da hali tasdiqlanmagan ([`docs/com-names.md`](docs/com-names.md)); M6 da tekshiriladi.

Qoidalar: bo'sh qiymat = standart; mantiqiy qiymatlar `1/0`, `true/false`, `yes/no`, `on/off`; noma'lum `VISSIM_*`
o'zgaruvchi (masalan, `VISSIM_ALLOWED_DIR`) — ogohlantirish va taklif. Qiymatni o'zgartirish: o'rnatuvchini kerakli
parametr bilan qayta ishga tushiring (`-ProjectsDir`, `-ProgId`, `-ExtraEnv`), keyin `/mcp` → Reconnect. Windows
foydalanuvchi muhit o'zgaruvchilari ham serverga meros bo'lib o'tadi, lekin ro'yxatdagi `env` ularni bosib ketadi.

## Claude Code'da ishlatish

Odatdagi sessiya (Vissim loyiha papkasida, masalan `D:\VissimProjects`):

1. `vissim_health` — server holati (COM'siz, darhol).
2. `vissim_connect` — o'zining yangi Vissim nusxasini ochadi (foydalanuvchi ochgan oynaga ulanmaydi).
3. `vissim_load_network` — `models\original\...inpx` ni yuklash (o'qish mumkin).
4. `vissim_list_objects` / `vissim_get_attributes` — ma'lumotni o'qish; `vissim_set_attributes` `dry_run=true`
   bilan → tasdiq → yozish.
5. `vissim_save_network` — yangi nomga saqlash (masalan `work\variant1.inpx`; mavjud fayl ustiga yozilsa,
   avval `backups\` ga nusxa).
6. `vissim_configure_simulation` → `vissim_start_simulation` (darhol `job_id`) → `vissim_wait_simulation`
   (takror, har biri ≤ 90 s) → `vissim_export_csv`.
7. `vissim_close` — litsenziya seat'ini bo'shatish.

22 ta tool:

| Guruh | Tool | Vazifasi | Turi |
|---|---|---|---|
| Hayot sikli | `vissim_connect` | Yangi Vissim nusxasini COM orqali ochish (PID, Job Object) | yozuvchi |
| | `vissim_health` | Server/ulanish holati, sozlamalar, versiya — COM'siz | o'qish |
| | `vissim_info` | Asosiy kolleksiyalar soni, `SimPeriod`, `SimRes` | o'qish |
| | `vissim_restart` | O'z Vissim'ini o'ldirib qayta ulanish (osilgan/qulagan holatda) | buzuvchi |
| | `vissim_close` | `Exit()` → kutish → terminate/kill; saqlamaydi | buzuvchi |
| Fayllar | `vissim_load_network` | `.inpx` (+ `.layx`) yuklash | yozuvchi |
| | `vissim_save_network` | `.inpx` saqlash, zaxira nusxa, sha256, audit | yozuvchi |
| Obyektlar | `vissim_describe` | Obyekt a'zolari (COM type info) | o'qish |
| | `vissim_list_objects` | Kolleksiya atributlari jadvali, sahifalab | o'qish |
| | `vissim_get_attributes` | Bitta obyekt atributlari | o'qish |
| | `vissim_set_attributes` | Atributlarni yozish (`dry_run`), audit | yozuvchi |
| | `vissim_set_attribute_bulk` | Ko'p obyektga bitta atribut (≤ 5000), audit | yozuvchi |
| Simulyatsiya | `vissim_configure_simulation` | Davr, aniqlik, urug', tezlik, QuickMode | yozuvchi |
| | `vissim_start_simulation` | Fon job: runlar, urug'lar, `pause_at` — darhol qaytadi | yozuvchi |
| | `vissim_simulation_status` | Job holati, foiz, ETA — COM'siz | o'qish |
| | `vissim_wait_simulation` | Job holati o'zgarguncha kutish (≤ `VISSIM_WAIT_MAX_S`) | o'qish |
| | `vissim_resume_simulation` | Pauzadan davom ettirish | yozuvchi |
| | `vissim_stop_simulation` | Keyingi bo'lak chegarasida to'xtatish | yozuvchi |
| Natijalar | `vissim_export_csv` | Kolleksiya atributlarini CSV'ga (`;`, utf-8-sig), audit | yozuvchi |
| Signallar | `vissim_set_signal_state` | Signal guruhi holati (faqat pauzada), kontrollerga qaytarish | yozuvchi |
| Diagnostika | `vissim_tail_log` | Server log oxiri (≤ 500 qator) | o'qish |
| | `vissim_exec_python` | Ixtiyoriy Python kodi — standart holatda **o'chiq** (`EXEC_DISABLED`) | buzuvchi |

Har bir javob — bitta JSON: `{"ok": true, ...}` yoki `{"ok": false, "error": {"code", "message", "hint"}}`,
ixtiyoriy `"warnings"`. Xato kodlari: `NOT_CONNECTED`, `ALREADY_CONNECTED`, `INVALID_ARGUMENT`, `PATH_DENIED`,
`READ_ONLY_PATH`, `FILE_NOT_FOUND`, `ATTRIBUTE_DENIED`, `RESOLVE_FAILED`, `COM_ERROR`, `TIMEOUT_HUNG`, `JOB_RUNNING`,
`JOB_NOT_FOUND`, `EXEC_DISABLED`, `INFO_UNAVAILABLE`, `SHUTTING_DOWN`, `INTERNAL`
([`docs/troubleshooting.md`](docs/troubleshooting.md)).

Tavsiya etilgan sozlamalar (PLAN 9.2–9.3):

- [`examples/settings.json.example`](examples/settings.json.example) → loyiha papkasidagi `.claude\settings.json`:
  o'qish toollari ruxsat etilgan (`mcp__vissim__<tool>`), `vissim_exec_python` taqiqlangan, qolganlari har safar
  tasdiq so'raydi.
- [`examples/CLAUDE.md.example`](examples/CLAUDE.md.example) → loyiha papkasidagi `CLAUDE.md`: ish tartibi
  qoidalari (avval `dry_run`, asl modellar faqat o'qiladi, oxirida `vissim_close` ...).
- [`examples/.mcp.json.example`](examples/.mcp.json.example) — faqat `project` scope kerak bo'lganda (yuqoriga qarang).

## Xavfsizlik modeli

- **Fayl tizimi — fail-closed.** Fayl operatsiyalari faqat `VISSIM_ALLOWED_DIRS` ichida (yo'l avval to'liq
  ochiladi: `..`, 8.3 qisqa nomlar, junction/symlink; `D:\A` va `D:\A2` farqlanadi). `models\original` — faqat
  o'qish. Kengaytmalar oq ro'yxati: yuklash `.inpx`/`.layx`, saqlash `.inpx`, eksport `.csv`. NTFS ADS rad etiladi.
- **Atributlar.** Nomida File/Path/Folder/Dir bo'lgan atributlarni yozish standart holatda taqiqlangan
  (`ATTRIBUTE_DENIED`) — fayl siyosatini chetlab o'tishning oldini olish uchun.
- **Kod bajarish.** `vissim_exec_python` faqat `VISSIM_MCP_ALLOW_EXEC=1` bilan; yoqilganda ham kod to'liq audit
  qilinadi. `settings.json.example` uni Claude Code darajasida ham taqiqlaydi.
- **Jarayonlar.** Server faqat o'zi COM orqali ochgan (`-Embedding`) va PID + ishga tushgan vaqt bilan tasdiqlangan
  Vissim'ni yopadi/o'ldiradi; siz o'zingiz ochgan Vissim oynasiga hech qachon tegmaydi. Server qanday tugamasin,
  kill-on-close Job Object o'z Vissim'ini Windows orqali yopadi (litsenziya seat'i band bo'lib qolmaydi).
- **Audit.** Har bir yozuvchi amal (muvaffaqiyatli, rad etilgan yoki xato) bitta JSONL yozuv: eski/yangi qiymatlar,
  saqlangan faylning sha256 va zaxira yo'li.
- **Maxfiylik (Q7).** Tool natijalari Claude modeli kontekstiga yuboriladi. Server faqat so'ralgan ustunlarni
  qaytaradi, katta jadvallar lokal CSV'da qoladi. Yuridik bo'lim qaror qilgunga qadar faqat test modellari bilan
  ishlang.
- **Huquqlar.** Server oddiy foydalanuvchi bilan ishlaydi. Administrator faqat bir martalik amallarga (COM
  register). Vissim'ni administrator sifatida ochiq qoldirmang — Job Object biriktirilmaydi.

## Log va audit

| Nima | Qayerda |
|---|---|
| Server log | `%LOCALAPPDATA%\vissim-mcp\logs\vissim-mcp.log` (5 MB × 5 aylanish; bir nechta sessiya bitta faylga yozadi) |
| Audit | `%LOCALAPPDATA%\vissim-mcp\audit\audit-YYYY-MM.jsonl` |
| Runtime venv | `%LOCALAPPDATA%\vissim-mcp\venv` |
| Claude Code MCP log | `claude --debug=mcp` (yoki `/mcp` dagi xato matni) |

Log oxirini Claude Code ichidan `vissim_tail_log` bilan, tashqaridan `doctor.ps1` bilan ko'rish mumkin. Log'ga
argument va atribut qiymatlari yozilmaydi (ular faqat auditda).

## Tekshirish, yangilash, o'chirish

```powershell
# "Kecha ishlagan edi, qayta yoqilgandan keyin ishlamayapti" — faqat o'qiydigan tekshiruv:
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

# Yangilash (git pull dan keyin) — xuddi o'rnatish kabi:
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# Ro'yxatdan chiqarish (venv, log va audit saqlanadi):
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
# ... venv'ni ham o'chirish / log va auditni ham o'chirish:
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -RemoveRuntime -RemoveData
```

`doctor.ps1` tekshiradi: runtime `python.exe` ishlaydi va 64-bit; o'rnatilgan versiya/kod repozitoriya bilan bir
xil; `claude mcp get vissim` runtime yo'liga ishora qiladi va ulanadi; ro'yxatdagi muhit bilan `--check`; MCP smoke
(22 tool, `vissim_health`); ProgID/CLSID va `LocalServer32` fayli; CodeMeter xizmati; Vissim litsenziyalari
(`cmu --list-network`); qolib ketgan COM Vissim jarayonlari; log papkasi yozilishi; log oxiri. Har bir
muvaffaqiyatsiz qator uchun tuzatish yo'li chiqadi; FAIL bo'lsa chiqish kodi `1`. Doctor hech narsani o'zgartirmaydi
(log papkasidagi bir lahzalik sinov faylidan tashqari), Vissim'ni ishga tushirmaydi va to'xtatmaydi. Parametrlar:
`-RuntimeDir`, `-ProjectsDir`, `-ProgId` (server ro'yxatda bo'lmaganda taxmin qilinadigan qiymatlar), `-NoClaude`
(`install.ps1 -NoRegister` dan keyin ro'yxat tekshiruvini o'tkazib yuborish), `-LogLines`. Hozir bu
kompyuterda "Vissim license" qatori kutilgan holda FAIL (Demo).

`uninstall.ps1` parametrlari: `-Scope` (standart `user`), `-RemoveRuntime`, `-RemoveData`, `-KeepRegistration`,
`-DryRun`. Loyiha papkasiga (`D:\VissimProjects`) hech qachon tegmaydi.

## Dasturchilar uchun

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider
.venv\Scripts\python.exe -X utf8 -m ruff check src tests scripts
```

Konsol kodirovkasi cp1251 bo'lgani uchun Python doim `-X utf8` bilan ishga tushiriladi. Dasturchi venv'i
(`.venv`, editable) va runtime venv (`%LOCALAPPDATA%\vissim-mcp\venv`, editable emas) alohida: Claude Code har doim
runtime'ni ishlatadi, shuning uchun ishlab chiqish jarayonidagi o'zgarishlar `install.ps1` qayta ishga
tushirilmaguncha serverga ta'sir qilmaydi.

Test qatlamlari (haqiqiy Vissim'siz; CI — Windows va Linux):

- `tests/unit` — config, errors, output, policy, resolver, audit, jobs, worker/service, winproc, barcha toollar,
  probe skripti, `register_claude.py` (soxta `tests/fake_claude.py` bilan — haqiqiy `claude.exe` hech qachon
  chaqirilmaydi) va `examples/*` ning server konfiguratsiyasiga mosligi.
- `tests/protocol` — server `python -X utf8 -m vissim_mcp` subprocess sifatida: `initialize` → `tools/list` →
  `tools/call`; stdout'dagi **har bir qator** JSON-RPC (ataylab `print()`, `os.write(1)`, `WriteFile` "shovqini"
  bilan ham); stdin yopilganda ≤ 300 ms chiqish; stand-in jarayon bilan PID/Job Object; `mcp_smoke.py` va
  `runtime_info.py`.
- `tests/real` — `@pytest.mark.vissim`, standart holatda o'tkazib yuboriladi; M6 da
  [`docs/m6-runbook.md`](docs/m6-runbook.md) bo'yicha.
- Soxta COM qatlami: `tests/fake_vissim.py` (`FakeVissim`), `tests/fakes/` (soxta `pythoncom`, `pywintypes`,
  `win32com.client.dynamic`), `tests/fake_backend.py`, `tests/fake_procs.py`. `tests/conftest.py`
  `VISSIM_MCP_FORBID_REAL=1` ni o'rnatadi — testlar haqiqiy COM yoki Vissim jarayoniga tegmaydi.

`scripts/`:

| Fayl | Vazifasi |
|---|---|
| `install.ps1`, `uninstall.ps1`, `doctor.ps1`, `_common.ps1` | O'rnatish, o'chirish, tekshirish (Windows PowerShell 5.1) |
| `register_claude.py` | `claude mcp add-json/remove/get` (argumentlar ro'yxati bilan, shell'siz); `--print-json`, `--show`, `--unregister`, `--dry-run` |
| `mcp_smoke.py` | `--check`, `--version`, MCP stdio smoke (22 tool, `vissim_health`) |
| `runtime_info.py` | Runtime haqida JSON: versiya, bitlik, bog'liqliklar, o'rnatilgan kod xeshi |
| `preflight.ps1`, `com_smoke.py` | M0 pre-flight (`-ComSmoke` haqiqiy Vissim'ni ochadi — faqat COM litsenziyasi bilan) |
| `probe_com_names.py` | M6: COM nomlarini haqiqiy Vissim'da tekshirish |

`mcp` SDK: o'rnatilgan versiya 1.30.0 (`pyproject.toml`: `mcp>=1.10,<2`). `stdio_server(stdin=, stdout=)` ikkala
parametrni qabul qiladi — PLAN 6.1 dagi asosiy yo'l ishlatilgan: `stdio_guard` fd 1 ni stderr'ga yo'naltiradi,
MCP faqat himoyalangan asl stdout nusxasiga yozadi. Barcha toollar `structured_output=False` bilan `CallToolResult`
qaytaradi; `ToolAnnotations` (`readOnlyHint`, `destructiveHint`) qo'yilgan.

## Cheklovlar va ochiq masalalar

- **M6 to'xtatilgan (Q2).** Kompyuterdagi yagona litsenziya — `PTV Vissim (Demo)`, COM moduli yo'q. Haqiqiy Vissim
  bilan ishlash, PLAN 10.3 testlari va `docs/com-names.md` dagi (*) nomlarni tasdiqlash COM moduli bor litsenziya
  (yoki PTV Hub obunasi) faollashtirilgandan keyin [`docs/m6-runbook.md`](docs/m6-runbook.md) bo'yicha bajariladi.
  Shungacha `vissim_connect` litsenziya oynasini aniqlab, tushunarli `COM_ERROR` qaytaradi.
- **`v1.0.0` tegi qo'yilmagan.** PLAN 10.4 qabul mezonlaridan "10.3 dagi 1–6 o'tkazilgan", "com-names yakunlangan"
  va "sessiya yopilgandan keyin `Vissim*.exe` qolmaydi" (haqiqiy Vissim bilan) M6 ga bog'liq.
- **Q7 (ma'lumot siyosati) ochiq.** Yuridik bo'lim tasdig'igacha faqat test modellari.
- Tasdiqlanmagan nomlar: `VISSIM_SEED_ATTR` (`RandSeed`), `VISSIM_BREAK_DISABLE_VALUE` (`0`) va boshqalar — sozlanadi.
- v1.0 doirasida: bitta Vissim nusxasi; foydalanuvchi ochgan oynaga ulanish (attach) yo'q; HTTP transport yo'q;
  Windows xizmati sifatida ishlamaydi.
- Claude Code stdio serverlarni avtomatik qayta ulamaydi: qayta o'rnatish yoki server qulaganidan keyin `/mcp` →
  Reconnect.
- Muammolar va yechimlar: [`docs/troubleshooting.md`](docs/troubleshooting.md).
