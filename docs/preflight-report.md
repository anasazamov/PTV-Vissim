# Pre-flight hisobot (M0)

Sana: 2026-10-16. Kompyuter: `DESKTOP-88D92K2` (Windows 11 Pro 10.0.26200, rus lokali, ANSI kod sahifasi 1251).

## Xulosa

| Tekshiruv | Natija |
|---|---|
| Vissim o'rnatilgan | ✅ PTV Vissim 2026 (`C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe`, fayl versiyasi 2026.0.10.0) |
| COM server ro'yxatdan o'tgan | ✅ `VISSIM.Vissim` → CLSID `{A93F962D-8DE3-48FC-9F71-B12A0F730FA3}`, `LocalServer32` = `Vissim260.exe`, TypeLib `{954DB7EB-6CE3-4594-948E-677DFD4ACDCD}` v1a.0 |
| Python 64-bit | ✅ 3.12.10 64-bit (`py -3.12`); tizimda 3.15.0 ham bor, lekin pywin32/pydantic g'ildiraklari uchun 3.12 tanlandi |
| CodeMeter | ✅ `CodeMeter.exe` xizmati ishlayapti, avtomatik ishga tushadi |
| **Litsenziyada COM moduli** | ❌ **Yo'q.** CodeMeter'da yagona Vissim litsenziyasi — `PTV Vissim (Demo)` (FC 6000459, PC 20000, 1 ta o'rin). Demo litsenziyada COM moduli bo'lmaydi |
| Minimal `Dispatch → Net.Links.Count → Exit` | ❌ COM orqali ochilgan nusxa (`Vissim260.exe -Embedding`) "Управление лицензиями – PTV Vissim" oynasini ko'rsatdi: *"Лицензия не найдена. Нажмите на (+), чтобы активировать новую лицензию, или укажите сервер лицензий…"* (License not found). Natija: `CO_E_SERVER_EXEC_FAILURE` (0x80080005, −2146959355), Windows System log'da DCOM 10010 ("server did not register with DCOM within the required timeout") |

**To'xtatuvchi masala (PLAN.md 12-bo'lim, 1-xavf):** haqiqiy Vissim'ni COM orqali boshqarish uchun COM moduli bor to'liq
litsenziya kerak (PTV distribyutoridan). Litsenziya oynasidagi "Nach Lizenzen aus Subscriptions suchen" bandi PTV Hub
obunasi orqali litsenziya qidirish imkonini beradi — agar Kompaniyada obuna bo'lsa, o'sha yerda faollashtirish mumkin.
Litsenziya faollashtirilgandan keyin uni "default" qilib saqlang (ishga tushishda tanlash dialogi chiqmasin) va
`scripts\preflight.ps1 -ComSmoke` ni qayta ishga tushiring.

Server (M1–M5, M7) baribir to'liq quriladi va soxta (fake) COM qatlami bilan sinovdan o'tkaziladi (PLAN.md 0-bo'lim,
6-tamoyil). M6 (haqiqiy Vissim'da tekshirish) litsenziya muammosi hal bo'lgunga qadar to'xtatib turiladi.

## Muhim kuzatuvlar (dizaynga ta'siri)

1. **Litsenziya dialogi COM chaqiruvini 75–120 s bloklaydi**, keyin `CO_E_SERVER_EXEC_FAILURE` qaytadi va dialogli
   jarayon o'z-o'zidan yopilmasligi mumkin. `vissim_connect` buni aniqlaydi: yangi `-Embedding` jarayonning ko'rinadigan
   oynalarini kuzatadi, litsenziya oynasi chiqsa jarayonni (faqat o'zi ochganini) yopadi va aniq xato qaytaradi.
2. **Foydalanuvchi o'zi ochgan Vissim** (`-Embedding`siz, masalan PID 6264) hech qachon o'ldirilmaydi: PID faqat
   `Dispatch` dan oldingi/keyingi ro'yxatlar farqi + `-Embedding` buyruq qatori orqali aniqlanadi.
3. **Lokalizatsiya:** COM xato matnlari ruscha keladi (`Ошибка при выполнении приложения-сервера`) — server xatolarni
   HRESULT bo'yicha tasniflaydi, matnga tayanmaydi. Konsol kodirovkasi cp1251 — server `PYTHONUTF8=1` va `-X utf8` bilan
   ishga tushiriladi.
4. **pywin32 `dynamic.Dispatch`** type library orqali `AttValue`, `SetAttValue`, `ItemByKey`, `GetMultipleAttributes`,
   `Count`, `Net`, `Simulation` a'zolarini to'g'ri yaratishi oflayn tekshirildi (`win32com.client.build.DispatchItem`).
   `gencache`/makepy keshi ishlatilmaydi (kesh buzilishi qayta ishga tushirishdan keyingi nosozliklarning klassik sababi).
5. **`mcp` SDK 1.30.0** (`>=1.10,<2`): `mcp.server.stdio.stdio_server(stdin=…, stdout=…)` ikkala parametrni ham qabul qiladi
   — PLAN.md 6.1 dagi asosiy yo'l ishlaydi, muqobil yo'l kerak emas.

## Q1–Q7 javoblari

Loyiha egasining aniq javobi bo'lmagan savollarda PLAN.md 2-bo'limdagi standart qiymat qo'llandi.

| # | Savol | Javob / holat |
|---|---|---|
| Q1 | Vissim versiyasi | **Aniqlandi:** faqat Vissim 2026 (2026.0.10.0). ProgID `Vissim.Vissim` (versiyaga xos: `Vissim.Vissim.2026`, `Vissim.Vissim-64.26`, … — hammasi bitta CLSID) |
| Q2 | Litsenziya turi, COM bormi | **Aniqlandi:** CodeMeter'da faqat `PTV Vissim (Demo)` — COM **yo'q**. Loyiha egasi to'liq litsenziya (COM moduli bilan) yoki PTV Hub obunasini tekshirishi kerak |
| Q3 | Claude Code va Vissim bitta kompyuterdami | **Ha** (Claude Code 2.1.301 shu kompyuterda, native Windows) |
| Q4 | Loyihalar katalogi | Standart: `D:\VissimProjects`, asl modellar `D:\VissimProjects\models\original\` (faqat o'qish) |
| Q5 | Sessiya tugaganda Vissim majburan yopilsinmi | Standart: **ha** (`VISSIM_KILL_ON_EXIT=1` + Job Object) |
| Q6 | GUI ko'rinsinmi yoki maksimal tezlik | Standart: maksimal tezlik (`UseMaxSimSpeed`, QuickMode, `SuspendUpdateGUI`) |
| Q7 | Tarmoq ma'lumotlarini Anthropic API'ga yuborish siyosati | **Ochiq.** Yuridik bo'lim tasdig'igacha faqat test modellari bilan ishlanadi |

## Avtomatik tekshiruvlar

`scripts\preflight.ps1` quyidagi bo'limni yangilaydi (qolgan qismi o'zgarmaydi):

<!-- preflight:auto:start -->
Generated: 2026-10-16 12:05:48 +05:00 on DESKTOP-88D92K2

### Windows
- Майкрософт Windows 11 Pro 10.0.26200 (64-разрядная)
- ANSI code page: 1251 (vissim-mcp forces UTF-8 via PYTHONUTF8=1 / -X utf8)

### Python
- Interpreter: D:\PTV-Vissim\.venv\Scripts\python.exe
- Version: 3.12.10, 64-bit
- Modules: pythoncom=True,win32com=True,psutil=True,mcp=True

### Vissim COM registration
- `VISSIM.Vissim` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim-64` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim-64.26` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim-64.260` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim-64.2600` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim.2026` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim.26` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim.260` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- `VISSIM.Vissim.2600` -> {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}
- CLSID {A93F962D-8DE3-48FC-9F71-B12A0F730FA3}: LocalServer32 = `C:\Program Files\PTV Vision\PTV Vissim 2026\exe\Vissim260.exe`, TypeLib = {954DB7EB-6CE3-4594-948E-677DFD4ACDCD}
- Executable version: 2026.0.10.0

### Running Vissim processes
- PID 6264 Vissim260.exe (interactive)

### CodeMeter and licenses
- Service CodeMeter.exe: Running (Automatic)
- License: PTV Vissim (Demo) (used 1, free 0, total 1)
- License: PTV Base License (UFC 6000459) (used 0, free 1, total 1)

### COM smoke test (Dispatch -> Net.Links.Count -> Exit)
- FAILED: license dialog shown by the COM-launched Vissim: Управление лицензиями - PTV Vissim

### Summary
- PROBLEM: Only Demo/Student Vissim licenses found - these do not include the COM module
- PROBLEM: No Vissim license with the COM module is available to COM-launched instances
<!-- preflight:auto:end -->
