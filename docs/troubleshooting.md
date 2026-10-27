# vissim-mcp — muammolar va yechimlar

Birinchi qadam har doim:

```powershell
cd D:\PTV-Vissim
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
```

Doctor faqat o'qiydi (Vissim'ni ochmaydi va yopmaydi) va har bir FAIL qatori uchun tuzatish yo'lini ko'rsatadi.
Ko'pincha yechim — o'rnatuvchini qayta ishga tushirish:
`powershell -ExecutionPolicy Bypass -File scripts\install.ps1`, keyin Claude Code'da `/mcp` → `vissim` → Reconnect.
O'rnatuvchi joriy ro'yxatdagi `-ProjectsDir`/`-ProgId`/`-ExtraEnv` va runtime papkasini o'zi saqlab qoladi
(ekranda "kept ..." qatori). `-Force` (venv'ni noldan yaratish) internet/proxy talab qiladi va serverdan
foydalanayotgan sessiyalar yopiq bo'lishi kerak; muvaffaqiyatsiz bo'lsa eski venv joyiga qaytariladi.

Tool xatosi doim `{"ok": false, "error": {"code", "message", "hint"}}` ko'rinishida keladi; `hint` dagi tavsiyani
birinchi bo'lib bajaring. COM xato matnlari Windows tilida (bu kompyuterda ruscha) keladi — server ularni HRESULT
raqami bo'yicha tasniflaydi, shuning uchun quyida HRESULT ko'rsatilgan.

## Mundarija

1. [Litsenziya oynasi / `CO_E_SERVER_EXEC_FAILURE`](#1-litsenziya-oynasi--co_e_server_exec_failure)
2. [`CO_E_CLASSSTRING` — COM server ro'yxatdan o'tmagan](#2-co_e_classstring--com-server-royxatdan-otmagan)
3. [`TIMEOUT_HUNG` — modal dialog](#3-timeout_hung--modal-dialog)
4. [`RPC_E_DISCONNECTED` — Vissim yopildi yoki quladi](#4-rpc_e_disconnected--vissim-yopildi-yoki-quladi)
5. [`PATH_DENIED`, `READ_ONLY_PATH`, `ATTRIBUTE_DENIED`](#5-path_denied-read_only_path-attribute_denied)
6. [`/mcp` da server yo'q yoki "failed"](#6-mcp-da-server-yoq-yoki-failed)
7. [Kompyuter qayta yoqilgandan keyin ishlamayapti](#7-kompyuter-qayta-yoqilgandan-keyin-ishlamayapti)
8. [Python va pywin32](#8-python-va-pywin32)
9. [Antivirus / EDR](#9-antivirus--edr)
10. [ASCII bo'lmagan yo'llar](#10-ascii-bolmagan-yollar)
11. [Log'lar qayerda](#11-loglar-qayerda)
12. [`/mcp` Reconnect qachon kerak](#12-mcp-reconnect-qachon-kerak)
13. [Boshqa xato kodlari](#13-boshqa-xato-kodlari)

## 1. Litsenziya oynasi / `CO_E_SERVER_EXEC_FAILURE`

**Belgisi:** `vissim_connect` 30–120 s kutib, `COM_ERROR` qaytaradi: HRESULT `-2146959355` (`0x80080005`,
`CO_E_SERVER_EXEC_FAILURE`, ruscha "Ошибка при выполнении приложения-сервера"), yoki xabarda "license dialog".
Ekranda "Управление лицензиями – PTV Vissim" / "License not found" oynasi chiqqan bo'lishi mumkin. Windows System
log'ida DCOM 10010.

**Sababi:** COM orqali ochilgan Vissim litsenziya topa olmadi:

- litsenziyada **COM moduli yo'q** (Demo/Student — bu kompyuterda hozir aynan shunday, PLAN Q2);
- litsenziya "default" qilib saqlanmagan, Vissim ishga tushishda tanlash dialogini ko'rsatadi;
- bitta o'rinli litsenziyani siz ochgan Vissim oynasi band qilib turibdi;
- CodeMeter xizmati ishlamayapti (7-bo'lim).

**Yechim:**

1. `doctor.ps1` → "Vissim license" va "CodeMeter" qatorlari.
2. COM moduli bor to'liq litsenziyani (yoki PTV Hub obunasini) faollashtiring: Vissim → Help → License, uni
   **default** qilib saqlang. Vissim'ni oddiy ochib tekshiring — dialog chiqmasligi kerak.
3. Single-seat litsenziyada o'zingiz ochgan Vissim oynalarini yoping.
4. Qayta urinish: `vissim_connect`. Server litsenziya oynasini o'zi aniqlaydi va **faqat o'zi ochgan** jarayonni
   yopadi; siz ochgan Vissim'ga tegmaydi.
5. To'liq tekshiruv: `powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -ComSmoke` (bu haqiqiy
   Vissim'ni ochadi — faqat COM litsenziyasi bo'lganda).

## 2. `CO_E_CLASSSTRING` — COM server ro'yxatdan o'tmagan

**Belgisi:** `COM_ERROR`, HRESULT `-2147221005` (`0x800401F3`, "Invalid class string"); doctor'da "COM ProgID" FAIL.

**Yechim:**

- Vissim'ni **administrator sifatida** oching → Help → Register COM Server → Vissim'ni yoping.
- Bir nechta Vissim versiyasi bo'lsa yoki `Vissim.Vissim` boshqa versiyaga ishora qilsa — versiyaga xos ProgID:
  `install.ps1 -ProgId Vissim.Vissim.2026` (ro'yxat: `scripts\preflight.ps1` hisobotidagi "Vissim COM registration").
- `VISSIM_PROGID` da xato yozilgan bo'lsa server ishga tushganda (exit 2) yoki `--check` da aytadi.

## 3. `TIMEOUT_HUNG` — modal dialog

**Belgisi:** tool `TIMEOUT_HUNG` qaytaradi ("Vissim'da modal dialog ochiq bo'lishi mumkin"); `vissim_health` da
`busy_seconds` o'sib boradi, `current_task` — osilib qolgan vazifa nomi. Keyingi COM chaqiruvlar ham timeout'ga
tushadi (COM chaqiruvini to'xtatib bo'lmaydi).

**Sababi:** Vissim oynasida modal dialog (masalan "Save changes?", xato xabari, litsenziya ogohlantirishi) javob
kutmoqda, yoki juda katta tarmoq `VISSIM_CALL_TIMEOUT_S` / `VISSIM_LOAD_TIMEOUT_S` dan uzoq yuklanmoqda.

**Yechim:**

1. Vissim oynasini toping (COM orqali ochilgan nusxa ham odatda ekranda yoki vazifalar panelida bo'ladi) va
   dialogni yoping — worker o'zi bo'shaydi.
2. Bo'lmasa: `vissim_restart` — o'z Vissim'ini o'ldiradi va yangisini ochadi (MCP serverni qayta ishga tushirish
   shart emas). Saqlanmagan o'zgarishlar yo'qoladi.
3. Katta tarmoqlar uchun vaqtni oshiring: `install.ps1 -ExtraEnv 'VISSIM_LOAD_TIMEOUT_S=1200'`, keyin `/mcp` →
   Reconnect.

## 4. `RPC_E_DISCONNECTED` — Vissim yopildi yoki quladi

**Belgisi:** `COM_ERROR`, HRESULT `-2147417848` (`0x80010108`), yoki `-2147023174` (`RPC_S_SERVER_UNAVAILABLE`),
`-2147220995` (`CO_E_OBJNOTCONNECTED`); `vissim_health` da `connected: false` yoki `vissim_alive: false`.

**Sababi:** Vissim jarayoni tugagan (qulagan, Task Manager'dan yopilgan, foydalanuvchi oynani yopgan).

**Yechim:** `vissim_restart` (yoki `vissim_connect`). Ishlab turgan simulyatsiya job'i `aborted` bo'ladi —
qaytadan `vissim_start_simulation`. `RPC_S_CALL_FAILED` (`-2147023170`) — javob yo'lda yo'qolgan; server jarayonni
1 s kuzatadi va tugagan bo'lsa ulanishni uzilgan deb belgilaydi.

## 5. `PATH_DENIED`, `READ_ONLY_PATH`, `ATTRIBUTE_DENIED`

| Kod | Ma'nosi | Yechim |
|---|---|---|
| `PATH_DENIED` | Yo'l `VISSIM_ALLOWED_DIRS` dan tashqarida yoki ildiz umuman sozlanmagan (fail-closed) | Faylni `D:\VissimProjects` ichiga ko'chiring yoki `install.ps1 -ProjectsDir <papka>`. `vissim_health` → `settings.VISSIM_ALLOWED_DIRS` ni tekshiring. Junction/symlink orqali tashqariga chiqish ham rad etiladi |
| `READ_ONLY_PATH` | `models\original` (yoki `VISSIM_READONLY_DIRS`) ichiga yozish/saqlash | Boshqa nomga, masalan `D:\VissimProjects\work\` ga saqlang. Asl modellar ataylab himoyalangan |
| `ATTRIBUTE_DENIED` | Nomida File/Path/Folder/Dir bo'lgan atributni yozish | Fayl siyosatini chetlab o'tishdan himoya. Haqiqatan kerak bo'lsa (test kompyuterida): `-ExtraEnv 'VISSIM_ALLOW_PATH_ATTRS=1'` |
| `INVALID_ARGUMENT` (kengaytma) | Yuklash faqat `.inpx`/`.layx`, saqlash `.inpx`, eksport `.csv` | To'g'ri kengaytma bering |
| `FILE_NOT_FOUND` | Fayl yo'q | Yo'lni tekshiring; nisbiy yo'l birinchi ruxsat etilgan ildizga nisbatan ochiladi |

## 6. `/mcp` da server yo'q yoki "failed"

1. `powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1` — "claude registration", "MCP smoke" qatorlari.
2. `claude mcp list` va `claude mcp get vissim`: `Scope` (`User config` bo'lishi kerak), `Command` — runtime
   `python.exe` (`%LOCALAPPDATA%\vissim-mcp\venv\Scripts\python.exe`), `Status`.
3. Ro'yxatda yo'q → `install.ps1`. Boshqa scope'da (`Local config` / `Project config`) eski yozuv bor → o'rnatuvchi
   (chiqish kodi 3) va doctor buni uy papkasidan ham, loyiha papkasidan (`VISSIM_ALLOWED_DIRS`, masalan
   `D:\VissimProjects\.mcp.json`) ham aniqlaydi va buyruqni aytadi, masalan
   `cd D:\VissimProjects; claude mcp remove vissim -s local`; keyin `install.ps1`. Bitta nom ikki scope'da bo'lsa,
   `local` > `project` > `user` ustuvor; Desktop ilovasining Code yorlig'i esa `~/.claude.json` (user) ni afzal
   ko'radi — natijada turli joyda turli server ishlaydi. `claude mcp get vissim` ni Claude Code ochiladigan papkada
   bajaring — u yerdan ko'rinadigan yozuv muhim.
4. `.mcp.json` (project scope) ishlatilsa — birinchi ochilganda Claude Code tasdiq so'raydi ("Pending approval").
5. Batafsil log: `claude --debug=mcp` bilan ishga tushiring — serverning stderr'i va ulanish xatosi ko'rinadi.
   Server stderr'iga faqat WARNING va undan yuqorisi chiqadi; to'liq log — 11-bo'lim.
6. Server qo'lda: `& "$env:LOCALAPPDATA\vissim-mcp\venv\Scripts\python.exe" -X utf8 -P -m vissim_mcp --check`
   (konfiguratsiya, chiqish kodi 2 = xato o'zgaruvchi nomi bilan) va `--version`. Diqqat: bu buyruq **joriy
   PowerShell oynasidagi** o'zgaruvchilarni tekshiradi, ro'yxatdagi `env` ni emas (`VISSIM_ALLOWED_DIRS` bo'sh
   bo'lgani uchun "no allowed directories" ogohlantirishi chiqishi tabiiy). Claude Code ishlatadigan muhitni
   `doctor.ps1` ning "configuration" qatori tekshiradi. Qo'lda tekshirish kerak bo'lsa, avval o'sha oynada
   `claude mcp get vissim` dagi qiymatlarni o'rnating, masalan `$env:VISSIM_ALLOWED_DIRS = "D:\VissimProjects"`.

## 7. Kompyuter qayta yoqilgandan keyin ishlamayapti

Server o'zi hech narsani "eslab" qolmaydi — Claude Code har sessiyada uni yangidan ishga tushiradi. Shuning uchun
qayta yoqishdan keyingi muammolar tashqi sabablardan:

| Belgisi | Sababi | Yechim |
|---|---|---|
| `vissim_connect` litsenziya xatosi, kecha ishlagan | **CodeMeter** xizmati ishga tushmagan yoki tarmoq litsenziya serveri hali ko'rinmaydi | `doctor.ps1` → "CodeMeter". Administrator: `Start-Service CodeMeter`, `services.msc` da start type = Automatic. Tarmoq litsenziyasi: bir daqiqa kutib qayta urining |
| `/mcp` da `vissim` failed, doctor'da "runtime python" FAIL | Asosiy Python o'chirilgan, ko'chirilgan yoki boshqa minor versiyaga yangilangan (venv unga bog'liq) | `install.ps1` (venv avtomatik qayta yaratiladi; internet/proxy kerak) yoki `install.ps1 -Force` |
| "installed code" WARN | Repozitoriya yangilangan, runtime eski | `install.ps1`, keyin `/mcp` → Reconnect |
| Vissim seat band, "Vissim processes" WARN | Oldingi sessiyadan `-Embedding` Vissim qolgan (masalan, server yoki Claude Code qulagan/o'ldirilgan va Job Object biriktirilmagan edi) | Yangi sessiyadagi `vissim_close`/`vissim_restart` uni yopa **olmaydi** — server faqat o'zi ochgan Vissim'ni boshqaradi, `vissim_restart` esa yana bir nusxa ochadi. Barcha Claude Code sessiyalarini yoping, keyin Task Manager → Details → faqat doctor ko'rsatgan PID → End task |
| Log papkasi FAIL | Ruxsat yoki antivirus | 9-bo'lim |

Runtime venv repozitoriyadan tashqarida (`%LOCALAPPDATA%\vissim-mcp\venv`) va paket editable emas — shuning uchun
`git checkout`/`git clean` yoki `.venv` ni o'chirish serverni buzmaydi.

## 8. Python va pywin32

- Faqat **64-bit** Python 3.12 (3.13, 3.11). 32-bit Python 64-bit Vissim COM serveri bilan ishlaydi, lekin
  qo'llab-quvvatlanmaydi; o'rnatuvchi 64-bit'ni talab qiladi. Python 3.14+ uchun `pywin32`/`pydantic` g'ildiraklari
  bo'lmasligi mumkin.
- `py -0p` — o'rnatilgan versiyalar ro'yxati. `py` launcher yo'q bo'lsa: python.org o'rnatuvchisini "py launcher"
  bilan qayta ishga tushiring yoki `install.ps1 -Python C:\...\python.exe`.
- `ImportError: DLL load failed` (`pywintypes`/`pythoncom`) → `install.ps1 -Force` (venv qayta yaratiladi). pywin32
  pip'dan o'rnatiladi; `pywin32_postinstall` (administrator) odatda shart emas.
- `PYTHONPATH` / `PYTHONHOME` o'rnatilgan bo'lsa, server boshqa koddan import qilishi mumkin — o'rnatuvchi buni
  aniqlaydi ("vissim_mcp is imported from ..."). Bu o'zgaruvchilarni foydalanuvchi muhitidan olib tashlang.
- `gencache`/makepy keshi ishlatilmaydi — `%TEMP%\gen_py` keshini o'chirish kerak emas.

## 9. Antivirus / EDR

Zanjir `claude.exe → python.exe (runtime venv) → DCOM → Vissim260.exe -Embedding` ba'zi EDR'larga shubhali
ko'rinadi. Belgilari: `vissim_connect` `E_ACCESSDENIED` (`-2147024891`), Job Object biriktirilmadi ogohlantirishi,
venv fayllari karantinga tushgan, log papkasiga yozib bo'lmaydi. IT bo'limiga quyidagi yo'llarni istisno uchun
bering: `%LOCALAPPDATA%\vissim-mcp\` (venv, logs, audit), `C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\`,
`D:\VissimProjects\`. Keyin `install.ps1 -Force` va `doctor.ps1`.

## 10. ASCII bo'lmagan yo'llar

Server UTF-8 bilan ishlaydi (`-X utf8`, `PYTHONUTF8=1`), lekin Vissim'ning o'zi kirill/o'zbekcha belgili
(`ko'cha`, `Йўл`) yo'llarni har doim ham o'qiy olmaydi. Bunday yo'lda server ogohlantiradi: "non-ASCII path; if
Vissim fails to read it, move the project to an ASCII path". Vissim fayl ochmasa yoki `COM_ERROR` bersa — loyihani
ASCII nomli papkaga ko'chiring (masalan `D:\VissimProjects\samarqand_kocha\`). Foydalanuvchi nomi kirillcha
bo'lsa ham `%LOCALAPPDATA%` dagi venv ishlaydi; muammo bo'lsa `install.ps1 -RuntimeDir C:\vissim-mcp\venv`.

## 11. Log'lar qayerda

| Nima | Yo'l |
|---|---|
| Server log | `%LOCALAPPDATA%\vissim-mcp\logs\vissim-mcp.log` (+ `.1` … `.5`) |
| Audit | `%LOCALAPPDATA%\vissim-mcp\audit\audit-YYYY-MM.jsonl` |
| Claude Code MCP | `claude --debug=mcp` chiqishi |
| Windows | Event Viewer → Windows Logs → System (DCOM 10010, 10016) |

Claude Code ichidan: `vissim_tail_log` (oxirgi ≤ 500 qator). Tashqaridan: `doctor.ps1` oxirgi qatorlarni ko'rsatadi
yoki `Get-Content "$env:LOCALAPPDATA\vissim-mcp\logs\vissim-mcp.log" -Tail 50 -Encoding UTF8`. Har bir qatorda
jarayon PID'i bor — bir nechta sessiya bitta faylga yozadi.

## 12. `/mcp` Reconnect qachon kerak

Claude Code stdio serverni avtomatik qayta ulamaydi. `/mcp` → `vissim` → Reconnect (yoki yangi sessiya):

- `install.ps1` / `uninstall.ps1` dan keyin;
- server jarayoni tugagan bo'lsa (masalan, antivirus o'ldirgan yoki Task Manager'dan yopilgan);
- konfiguratsiya (`-ExtraEnv`, `-ProjectsDir`) o'zgargandan keyin.

Vissim'ning o'zi qulasa yoki osilsa Reconnect **kerak emas** — `vissim_restart` yetarli. Per-call `timeout`
(900 000 ms) — bitta tool chaqiruvining chegarasi, server "idle" bo'lgani uchun o'chirilmaydi. 25 000 tokendan /
50 000 belgidan uzun natijani Claude Code faylga saqlaydi — katta jadvallar uchun `vissim_export_csv`.

## 13. Boshqa xato kodlari

| Kod | Ma'nosi va yechim |
|---|---|
| `NOT_CONNECTED` | Avval `vissim_connect` |
| `ALREADY_CONNECTED` | Ulanish bor; yangi nusxa kerak bo'lsa `vissim_restart` |
| `JOB_RUNNING` | Simulyatsiya ishlayapti: `vissim_wait_simulation` / `vissim_stop_simulation`; signal holati faqat pauzada (`pause_at`) |
| `JOB_NOT_FOUND` | `job_id` noto'g'ri yoki eski (oxirgi 20 tasi saqlanadi) |
| `RESOLVE_FAILED` | Obyekt yo'li topilmadi (`Net.Links[5]`): xabarda qaysi segmentda to'xtagani; `vissim_describe` bilan tekshiring |
| `INFO_UNAVAILABLE` | Obyektda COM type info yo'q — `vissim_get_attributes` bilan nom bo'yicha o'qing |
| `EXEC_DISABLED` | `vissim_exec_python` o'chiq (standart). Faqat test kompyuterida `VISSIM_MCP_ALLOW_EXEC=1` |
| `SHUTTING_DOWN` | Server yopilmoqda (sessiya tugayapti) — yangi sessiya oching |
| `INTERNAL` | Server ichki xatosi; tafsilot log'da (`vissim_tail_log`). Server ishlashda davom etadi |
