# Vissim COM nomlari va ularning tasdiq holati

PLAN.md 0-bo'lim, 5-tamoyil: **haqiqiy Vissim'da tasdiqlanmagan nom kodga qattiq yozilmaydi** — u konfiguratsiya
(muhit o'zgaruvchisi) yoki umumiy `get/set` tool orqali beriladi. Bu jadval har bir nomning holatini yuritadi.

## Holatlar

| Holat | Ma'nosi |
|---|---|
| `unverified` | Faqat taxmin; hech qanday manbada tekshirilmagan |
| `documented` | Vissim 2026 rasmiy manbalarida bor (pastdagi manbalar), lekin ishlayotgan Vissim'da hali sinalmagan |
| `verified` | `scripts/probe_com_names.py` yoki `tests/real` orqali haqiqiy Vissim'da tasdiqlangan |
| `rejected` | Haqiqiy Vissim'da ishlamadi; muqobil ko'rsatilgan |
| `configurable` | Kodda sozlanadigan parametr orqali beriladi (standart qiymat jadvalda) |

## Manbalar

- **[TL]** — ro'yxatdan o'tgan type library `{954DB7EB-6CE3-4594-948E-677DFD4ACDCD}` v1a.0 (Vissim 2026), `pythoncom.LoadRegTypeLib` bilan o'qilgan; pywin32 `build.DispatchItem` orqali `dynamic.Dispatch` a'zolari tekshirilgan.
- **[AX]** — `C:\Program Files\PTV Vision\PTV Vissim 2026\Doc\Eng\attribute.xlsx` ("Attributes" varag'i: obyekt, atribut, tahrirlanishi, simulyatsiya paytidagi xatti-harakati, qiymat turi, min/max/standart).
- **[EX]** — rasmiy misol `Examples Training\COM\Basic Commands\COM Basic Commands.py` va `Run 3 Simulations.vbs` (Vissim 2026).

> 2026-11-18 da M6 o'tkazildi: PTV Vissim Academic 2026 (SP 10), ruscha interfeys. Quyidagi nomlar
> `scripts/probe_com_names.py` va `tests/real` bilan haqiqiy Vissim'da tekshirilgan (docs/test-report-v1.md).
> 2026-10-16 gacha (M0–M5) faqat Demo litsenziya bor edi va hech bir nom `verified` emas edi.

## M6 da aniqlangan xatti-harakatlar (haqiqiy Vissim 2026)

| Nima | Kuzatilgan | Kodda |
|---|---|---|
| Mantiqiy atributlar | COM orqali `True/False` emas, butun son `1/0` keladi (`ContrByCOM`, `UseMaxSimSpeed`, `IsRunning`, `IsConn`) | `vissim_set_signal_state` `ContrByCOM` ni `true/false` ga aylantiradi; umumiy `get/list` toollari qiymatni o'zgartirmaydi |
| Enum atributlar | Katta harfli satr: `KeepPrevResults` = `"KEEPALL"`, `SigState` = `"REDAMBER"`; yozishda satr ham, raqam ham qabul qilinadi (`SigState=1` → `"RED"`) | `vissim_start_simulation` ogohlantirishi `KEEPALL`/`3` ni "hammasi saqlanadi" deb biladi |
| `SetAttValue` | Type library'da yo'q: bu pywin32 ning `AttValue` xossasiga yozish (property put) uchun nomi. `vissim_describe` `AttValue` ni `property_get` va `property_put` sifatida ko'rsatadi | o'zgarishsiz (pywin32 orqali ishlaydi) |
| `IAttribute.ID` / `Code` / `Name` | `ID` — tilga bog'liq bo'lmagan identifikator (`NO`, `NAME`, `LENGTH2D`; `AttValue` registrga befarq qabul qiladi). `Code` va `Name` — interfeys tilida (ruscha: `№`, `Имя`, `Длина2D`); `AttValue("№")` xato beradi | `vissim_describe(include_attributes=true)` har bir atribut uchun `id` qaytaradi |
| `SimBreakAt` va `IsSimBreakAtActive` | `SimBreakAt=N` yozilganda `IsSimBreakAtActive` o'zi `1` bo'ladi; `RunContinuous()` aynan N da to'xtaydi (`IsRunning=1`) | server `IsSimBreakAtActive` ni yozmaydi — kerak emas |
| `SimBreakAt=0` | Break yo'q: run oxirigacha boradi | `VISSIM_BREAK_DISABLE_VALUE=0` to'g'ri |
| Run tugagandan keyin | `SimSec=0`, `IsRunning=0`; `SimulationRuns` ga qator run **boshlanganda** qo'shiladi, `SimEnd` — yetib borilgan soniya | `jobs.py` run'dan keyingi `SimSec` ga tayanmaydi (o'zgarishsiz) |
| O'tib ketgan break (`SimSec=120` da `SimBreakAt=60`) | `RunContinuous()` run'ni oxirigacha olib boradi (`SimSec=0`, `IsRunning=0`) | job bunday holatdan to'g'ri tugaydi (`test_stale_break_does_not_break_a_job`) |
| `SigState` / `ContrByCOM` simulyatsiyadan tashqarida | O'qilganda bo'sh (`None`), yozib bo'lmaydi ("Атрибут … не может быть изменен") | `vissim_set_signal_state` bu holatda `pause_at` bilan pauza qilishni maslahat beradi |
| `SigState` pauzada | Yoziladi; `ContrByCOM` o'zi `1` bo'ladi; `ContrByCOM=False` kontrollerga qaytaradi | o'zgarishsiz |
| Litsenziya dialogi | To'g'ri litsenziya bilan ham taxminan har sakkizinchi ishga tushishda "Управление лицензиями - PTV Vissim" oynasi chiqdi (bir marta ketma-ket ikki bor); keyingi urinish odatda muvaffaqiyatli | `vissim_connect`/`vissim_restart` yana 2 martagacha urinadi |

Holat ustunini `scripts/probe_com_names.py --i-have-a-com-license --update` yangilaydi (ishlatish tartibi:
docs/m6-runbook.md). Skript har bir qatorni birinchi katakdagi nom bo'yicha topadi — qator nomini o'zgartirsangiz,
skriptdagi `PROBES` ro'yxatini ham o'zgartiring (`tests/unit/test_probe_com_names.py` buni tekshiradi).

<!-- probe:auto:start -->
> Oxirgi `scripts/probe_com_names.py` ishga tushirilishi: 2026-11-18 — 32 ta nom `verified`, 0 ta `rejected`, 0 ta tekshirilmadi. Xom natijalar: docs/test-report-v1.md.
<!-- probe:auto:end -->

## Metodlar va xossalar (COM interfeys a'zolari)

| Nom | Qayerda ishlatiladi | Manba | Holat |
|---|---|---|---|
| `Vissim.LoadNet(path, additive)` | `vissim_load_network` | TL, EX | verified |
| `Vissim.LoadLayout(path)` | `vissim_load_network` | TL, EX | verified |
| `Vissim.SaveNetAs(path)` | `vissim_save_network` | TL, EX | verified |
| `Vissim.Exit()` | `vissim_close` | TL | verified |
| `Vissim.SuspendUpdateGUI()` / `ResumeUpdateGUI(bool)` | `vissim_configure_simulation` | TL, EX | verified |
| `Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics` | resolver ildizlari | TL, EX | verified |
| `Graphics.CurrentNetworkWindow` | QuickMode | TL, EX | verified |
| `Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()` | `jobs.py` | TL, EX | verified |
| `<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)` | resolver, `vissim_info`, bulk | TL, EX | verified |
| `Net.Links`, `.Nodes`, `.SignalControllers`, `.SignalHeads`, `.VehicleInputs`, `.VehicleRoutingDecisionsStatic`, `.DataCollectionMeasurements`, `.VehicleTravelTimeMeasurements`, `.QueueCounters` | `vissim_info` (`Count`; har biri alohida o'qiladi, o'qib bo'lmasa `null` + `errors`) | TL (`INet` a'zolari, 2026-10-16 oflayn qayta tekshirildi) | verified |
| `<collection>.GetMultipleAttributes(tuple)` | `vissim_list_objects`, `vissim_export_csv` | TL, EX | verified |
| `<obj>.AttValue(name)` / `.SetAttValue(name, value)` | barcha get/set | TL, EX | verified |
| `<collection>.Attributes.GetAll()` → `IAttribute.Code/Name/ValueType/Editable` | `vissim_describe(include_attributes=true)` | TL | verified |
| `_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)` → `GetNames(memid)` | `vissim_describe` | TL (oflayn tekshirildi) | verified |
| `Net.SignalControllers[n].SGs[m]` | `vissim_set_signal_state` | TL, EX | verified |
| `Net.SimulationRuns` (`GetMultipleAttributes(('No','RandSeed','SimEnd'))`) | natijalar | TL, EX | verified |

## Atributlar

| Nom | Obyekt | Qayerda ishlatiladi | Manba | Holat | Izoh |
|---|---|---|---|---|---|
| `SimPeriod` | Simulation | configure, start | AX, EX | verified | min 1, max 2 678 400, standart 3600; simulyatsiya paytida faqat o'qish |
| `SimRes` | Simulation | configure, `jobs.py` (bitta vaqt qadami: run oxirini aniqlash) | AX | verified | 1–1000, standart 10 |
| `SimSec` | Simulation | `jobs.py` (bo'lak tekshiruvi) | AX | verified | faqat o'qish. Run tugagandan keyin qiymati (0 yoki period) — **noma'lum**, kod bunga tayanmaydi |
| `SimBreakAt` | Simulation | `jobs.py` | AX, EX | verified | min 0, standart 0, simulyatsiya paytida tahrirlanadi |
| `RandSeed` | Simulation | `VISSIM_SEED_ATTR` | AX, EX | verified → configurable | min 1 (EX: "RandSeed 0 is not allowed"), standart 42; simulyatsiya paytida faqat o'qish |
| `UseMaxSimSpeed` | Simulation | configure | AX, EX | verified | bool |
| `NumCores` | Simulation | configure `extra` orqali | AX | verified | min 1; `UseAllCores` (bool, standart true) bilan birga |
| `IsRunning` | Simulation | (diagnostika) | AX | verified | faqat o'qish; pauza paytidagi qiymati noma'lum |
| `IsSimBreakAtActive` | Simulation | — | AX | verified | **Vissim 2026 da yangi**, standart false. `SimBreakAt` bilan bog'liqligi noma'lum — M6 da tekshiriladi |
| `NumRuns`, `RandSeedIncr` | Simulation | ishlatilmaydi | AX | verified | server ko'p runni o'zi boshqaradi |
| `QuickMode` | NetworkWindow (`Graphics.CurrentNetworkWindow`) | configure | EX | verified | 1/0 |
| `KeepPrevResults` | Evaluation | `start` ogohlantirishi | AX, TL | verified | enum `KeepPreviousResults`: 1 KeepNone, 2 KeepCurrent, 3 KeepAll (standart KeepAll) |
| `SigState` | SignalGroup | `vissim_set_signal_state` | AX, EX, TL | verified | enum `SignalizationState`: 1 Red, 2 RedAmber, 3 Green, 4 Amber, 5 Off, 6 Undefined, 7 FlashingAmber, 8 FlashingRed, 9 FlashingGreen, 10 AlternatingRedGreen, 11 GreenAmber; EX satr bilan yozadi: `"GREEN"`, `"RED"`, `"AMBER"`, `"REDAMBER"` |
| `ContrByCOM` | SignalGroup | `vissim_set_signal_state` | AX, EX | verified | `SigState` yozilganda avtomatik `True`; `False` — kontrollerga qaytarish; faqat simulyatsiya paytida faol |

## Sozlanadigan qiymatlar (PLAN.md 5-bo'lim, (*) belgilari)

| O'zgaruvchi | Standart | Asos | Holat |
|---|---|---|---|
| `VISSIM_SEED_ATTR` | `RandSeed` | AX, EX | configurable (verified) |
| `VISSIM_BREAK_DISABLE_VALUE` | `0` | EX: "`SimBreakAt` = 0 means no break!", VBS: "If set to 0 then the simulation is not paused"; AX: min 0, standart 0 | configurable (verified) |

## Tarmoq qurish metodlari (haqiqiy Vissim 2026 da tekshirildi, 2026-11-18)

`vissim_call_method` chaqiradigan metodlar. "Sinalgan" — `tests/real/test_real_build_network.py` da haqiqiy Vissim'da
ishlatilgan; qolganlari type library'dan o'qilgan (`vissim_describe` ko'rsatadi), lekin ishga tushirib ko'rilmagan.
`Key = 0` — Vissim keyingi bo'sh raqamni o'zi tanlaydi; pozitsiyalar metrda; obyekt argumenti — `{"ref": "<yo'l>"}`.

| Metod | Qayerda | Tekshiruv |
|---|---|---|
| `Vissim.New()` | ildiz | sinalgan. **`New()` dan oldin olingan COM havolasi (`Net`) keyin ishlatilsa Vissim qulaydi** — server har chaqiruvda yo'lni ildizdan qayta ochadi |
| `AddLink(Key, WktLinestring, LaneWidths)` | `Net.Links` | sinalgan: `[0, "LINESTRING(0 0, 200 0)", [3.5, 3.5]]` → 2 polosali havola |
| `AddConnector(Key, FromLane, FromPos, ToLane, ToPos, NumberOfLanes, WktLinestring)` | `Net.Links` | sinalgan (`WktLinestring = "LINESTRING EMPTY"`); raqami 10000 dan boshlanadi |
| `RemoveLink(Link)`, `DuplicateLink(Source, NewKey)` | `Net.Links` | sinalgan |
| `AddVehicleInput(Key, Link)` | `Net.VehicleInputs` | sinalgan; hajm — `Volume(1)` atributi |
| `AddVehicleRoutingDecisionStatic(Key, Link, Pos)` | `Net.VehicleRoutingDecisionsStatic` | sinalgan |
| `AddVehicleRouteStatic(Key, DestLink, DestPos)` | `...[n].VehRoutSta` | sinalgan; ulush — `RelFlow(1)` |
| `AddSignalController(Key)`, `RemoveSignalController(SignalController)` | `Net.SignalControllers` | sinalgan; yangi kontroller turi `FIXEDTIME` |
| `AddSignalGroup(Key)` | `Net.SignalControllers[n].SGs` | sinalgan |
| `AddSignalHead(Key, Lane, Pos)`, `RemoveSignalHead(SignalHead)` | `Net.SignalHeads` | sinalgan; guruh — `SG` atributi `"<kontroller>-<guruh>"` |
| `AddDataCollectionPoint(Key, Lane, Pos)` | `Net.DataCollectionPoints` | sinalgan |
| `AddDataCollectionMeasurement(Key)` | `Net.DataCollectionMeasurements` | sinalgan; nuqtalar — `DataCollectionPoints` atributi (`"1"`) |
| `AddVehicleTravelTimeMeasurement(Key, StartLink, StartPos, EndLink, EndPos)` | `Net.VehicleTravelTimeMeasurements` | sinalgan |
| `AddNode(Key, WktPolygon)` | `Net.Nodes` | sinalgan (prototipda) |
| `AddDesSpeedDecision`, `AddReducedSpeedArea`, `AddStopSign`, `AddPriorityRule`, `AddQueueCounter`, `AddDetector`, `AddParkingLot`, `AddPTLine`, `AddPTStop`, `AddArea`, `AddPedestrianInput`, `AddSection`, `AddLevel`, `AddVehicleType`, `AddVehicleComposition`, `GenerateOppositeDirection` va ularning `Remove…`/`Duplicate…` juftlari | tegishli kolleksiyalar | type library'da bor, **sinalmagan** |

Baholash yozuvini yoqish (`vissim_set_attributes(path="Evaluation", ...)`): `DataCollCollectData`, `VehTravTmsCollectData`
— sinalgan.

## Svetofor dasturi (haqiqiy Vissim 2026 da tekshirildi)

| Nima | Kuzatilgan | Kodda |
|---|---|---|
| `SignalController.IntSupplyData` | Qat'iy vaqtli kontroller dasturi — base64 bilan kodlangan Vissig XML, tarmoq fayli ichida saqlanadi; COM orqali yoziladi, `.sig` fayl shart emas | `vissim_set_signal_program` XML ni tuzib shu atributga yozadi, `ProgNo = 1` |
| Dasturi yo'q kontroller | Run boshlanmaydi: "The signal groups defined in Vissim are different from those in the external data files" | xato `details.vissim_messages` da keladi |
| `<cmd display="3" begin=...>` / `<cmd display="1" begin=...>` | Yashil / qizil boshlanadigan lahza (ms). Qizil-sariq yashildan **oldin**, sariq qizildan **oldin** ko'rsatiladi | tool `green_end_s` ga sariq vaqtini qo'shib qizil boshlanishini hisoblaydi |
| Tekshiruv: sikl 60 s, yashil 10–30 s, sariq 3 s, qizil-sariq 1 s | 5 s — `RED`, 9 — `REDAMBER`, 20 — `GREEN`, 31 — `AMBER`, 34 — `RED`, 80 — `GREEN`, 100 — `RED` | `tests/real/test_real_build_network.py` |
| Dastur kontrollerning barcha guruhlarini qamrashi shart | aks holda yuqoridagi xato | tool ro'yxatda yo'q mavjud guruh uchun `INVALID_ARGUMENT`, yo'q guruhni o'zi yaratadi |

## Fayl, xabar va litsenziya xatti-harakatlari (haqiqiy Vissim 2026)

| Nima | Kuzatilgan | Kodda |
|---|---|---|
| Ildiz atributlari `InputFile`, `WorkingFolder` | Saqlangan tarmoqda `"net1.inpx"` va papka (oxirida `\`); fayli yo'q tarmoqda bo'sh | `vissim_start_simulation` bo'sh `InputFile` da rad etadi |
| Fayli yo'q tarmoqda `RunContinuous()` | Vissim "Файл сохранить как" dialogini ochadi, COM osiladi | yuqoridagi rad etish; `TIMEOUT_HUNG` xabarida ochiq oynalar nomi ko'rsatiladi |
| `<tarmoq>_NNN.err` | Run boshlanmaganda Vissim sababni shu faylga yozadi (tab bilan ajratilgan, UTF-8 BOM) | job xatosiga `vissim_messages` sifatida qo'shiladi |
| `%TEMP%\VISSIM\vissim_msgs.txt` | Vissim'ning joriy xabarlar jurnali (daraja nomlari interfeys tilida) | yuklash muvaffaqiyatsiz bo'lganda yangi, "odatiy bo'lmagan" qatorlar xatoga qo'shiladi |
| Run boshlanmay qolgandan keyin | Sabab tuzatilsa ham, `Stop()` dan keyin ham keyingi run "Не удалось инициализировать цикл имитации"; tarmoqni fayldan qayta yuklash yordam beradi | xato maslahati: saqlash → `vissim_load_network` |
| Academic litsenziya cheklovlari | "максимальный размер сети (10000 m) ... превышен", "Максимальное количество светосигнальных устройств (20) ... превышено" — tarmoq yuklanmaydi | xabar foydalanuvchiga yetkaziladi |

## Jarayon xatti-harakatlari (M6 da tekshirildi, 2026-11-18)

| Nima | Kodda | M6 natijasi |
|---|---|---|
| Job Object `LimitFlags` | Faqat `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (0x2000), PLAN 6.4 bo'yicha; `BREAKAWAY_OK` yo'q | Vissim va uning bola jarayonlari (7 ta `msedgewebview2.exe`) Job Object ichida; ishga tushishda xato yo'q. Server o'ldirilganda Vissim 1 s ichida yo'qoladi. Flag'ni o'zgartirish kerak emas |
| `Exit()` javobi | Javob qaytsa ham, `RPC_S_CALL_FAILED` / `RPC_E_DISCONNECTED` bilan tugasa ham jarayon 10 s kutiladi | `Exit()` javob qaytaradi, jarayon 3,7–6 s da tugaydi (`method: exit`); simulyatsiya pauzada turgan holatda ham |
| `ItemByKey` kalit turi | `vissim_set_attribute_bulk` kalitni o'zgartirmasdan uzatadi; `5` va `"5"` takror kalit sifatida rad etiladi | `Net.Links.ItemByKey("1")` va `ItemKeyExists("1")` ishlaydi va o'sha obyektni qaytaradi (`No=1`) — soxta kolleksiya xatti-harakati to'g'ri |

## Yo'l (fayl) atributlari — `ATTRIBUTE_DENIED` siyosati

PLAN.md 6.7: nomida (registrga befarq) `File`, `Path`, `Folder`, `Dir` bo'lgan atributni yozish
`VISSIM_ALLOW_PATH_ATTRS=0` da rad etiladi. AX bo'yicha `filename` turidagi barcha atributlar shu qoidaga tushadi:
`SignalController.SupplyFile1..3`, `ProgFile`, `GuiFile`, `Evaluation.EvalOutDir`, `DynamicAssignment.CostFile`,
`PathFile`, `TripChainFile`, `Script.ScriptFile`, `VehicleType.ExtDriverDLLFile`, `ExtDriverParFile`,
`ExtEmissionDLLFile`, `BackgroundImage.PathFilename`, `AnimRec.FileName`, `Static3DModel.ModelFilename`,
`Storyboard.Filename`, `WTTFile.File` va boshqalar.

Ma'lum soxta mosliklar (fayl bo'lmasa ham rad etiladi, chunki qoida fail-closed): `Link.Direction`,
`NetPara.NorthDir`, `ParkingLot.ParkDir`, `PavementMarking.ArrowDir`, `DynamicAssignment.PathSelMethod`,
`*.WriteFile` (bool) va h.k. Bunday atributni yozish zarur bo'lsa — `VISSIM_ALLOW_PATH_ATTRS=1` (ehtiyotkorlik bilan).
