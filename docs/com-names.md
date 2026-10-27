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

> 2026-10-16 holatiga ko'ra hech bir nom `verified` emas: kompyuterdagi yagona litsenziya — Demo (COM moduli yo'q),
> shuning uchun COM orqali Vissim ishga tushmaydi (docs/preflight-report.md). M6 litsenziya hal bo'lgach bajariladi.

Holat ustunini `scripts/probe_com_names.py --i-have-a-com-license --update` yangilaydi (ishlatish tartibi:
docs/m6-runbook.md). Skript har bir qatorni birinchi katakdagi nom bo'yicha topadi — qator nomini o'zgartirsangiz,
skriptdagi `PROBES` ro'yxatini ham o'zgartiring (`tests/unit/test_probe_com_names.py` buni tekshiradi).

<!-- probe:auto:start -->
> `scripts/probe_com_names.py` hali haqiqiy Vissim'da ishga tushirilmagan (COM litsenziyasi yo'q, Demo).
<!-- probe:auto:end -->

## Metodlar va xossalar (COM interfeys a'zolari)

| Nom | Qayerda ishlatiladi | Manba | Holat |
|---|---|---|---|
| `Vissim.LoadNet(path, additive)` | `vissim_load_network` | TL, EX | documented |
| `Vissim.LoadLayout(path)` | `vissim_load_network` | TL, EX | documented |
| `Vissim.SaveNetAs(path)` | `vissim_save_network` | TL, EX | documented |
| `Vissim.Exit()` | `vissim_close` | TL | documented |
| `Vissim.SuspendUpdateGUI()` / `ResumeUpdateGUI(bool)` | `vissim_configure_simulation` | TL, EX | documented |
| `Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics` | resolver ildizlari | TL, EX | documented |
| `Graphics.CurrentNetworkWindow` | QuickMode | TL, EX | documented |
| `Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()` | `jobs.py` | TL, EX | documented |
| `<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)` | resolver, `vissim_info`, bulk | TL, EX | documented |
| `Net.Links`, `.Nodes`, `.SignalControllers`, `.SignalHeads`, `.VehicleInputs`, `.VehicleRoutingDecisionsStatic`, `.DataCollectionMeasurements`, `.VehicleTravelTimeMeasurements`, `.QueueCounters` | `vissim_info` (`Count`; har biri alohida o'qiladi, o'qib bo'lmasa `null` + `errors`) | TL (`INet` a'zolari, 2026-10-16 oflayn qayta tekshirildi) | documented |
| `<collection>.GetMultipleAttributes(tuple)` | `vissim_list_objects`, `vissim_export_csv` | TL, EX | documented |
| `<obj>.AttValue(name)` / `.SetAttValue(name, value)` | barcha get/set | TL, EX | documented |
| `<collection>.Attributes.GetAll()` → `IAttribute.Code/Name/ValueType/Editable` | `vissim_describe(include_attributes=true)` | TL | documented |
| `_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)` → `GetNames(memid)` | `vissim_describe` | TL (oflayn tekshirildi) | documented |
| `Net.SignalControllers[n].SGs[m]` | `vissim_set_signal_state` | TL, EX | documented |
| `Net.SimulationRuns` (`GetMultipleAttributes(('No','RandSeed','SimEnd'))`) | natijalar | TL, EX | documented |

## Atributlar

| Nom | Obyekt | Qayerda ishlatiladi | Manba | Holat | Izoh |
|---|---|---|---|---|---|
| `SimPeriod` | Simulation | configure, start | AX, EX | documented | min 1, max 2 678 400, standart 3600; simulyatsiya paytida faqat o'qish |
| `SimRes` | Simulation | configure, `jobs.py` (bitta vaqt qadami: run oxirini aniqlash) | AX | documented | 1–1000, standart 10 |
| `SimSec` | Simulation | `jobs.py` (bo'lak tekshiruvi) | AX | documented | faqat o'qish. Run tugagandan keyin qiymati (0 yoki period) — **noma'lum**, kod bunga tayanmaydi |
| `SimBreakAt` | Simulation | `jobs.py` | AX, EX | documented | min 0, standart 0, simulyatsiya paytida tahrirlanadi |
| `RandSeed` | Simulation | `VISSIM_SEED_ATTR` | AX, EX | documented → configurable | min 1 (EX: "RandSeed 0 is not allowed"), standart 42; simulyatsiya paytida faqat o'qish |
| `UseMaxSimSpeed` | Simulation | configure | AX, EX | documented | bool |
| `NumCores` | Simulation | configure `extra` orqali | AX | documented | min 1; `UseAllCores` (bool, standart true) bilan birga |
| `IsRunning` | Simulation | (diagnostika) | AX | documented | faqat o'qish; pauza paytidagi qiymati noma'lum |
| `IsSimBreakAtActive` | Simulation | — | AX | documented | **Vissim 2026 da yangi**, standart false. `SimBreakAt` bilan bog'liqligi noma'lum — M6 da tekshiriladi |
| `NumRuns`, `RandSeedIncr` | Simulation | ishlatilmaydi | AX | documented | server ko'p runni o'zi boshqaradi |
| `QuickMode` | NetworkWindow (`Graphics.CurrentNetworkWindow`) | configure | EX | documented | 1/0 |
| `KeepPrevResults` | Evaluation | `start` ogohlantirishi | AX, TL | documented | enum `KeepPreviousResults`: 1 KeepNone, 2 KeepCurrent, 3 KeepAll (standart KeepAll) |
| `SigState` | SignalGroup | `vissim_set_signal_state` | AX, EX, TL | documented | enum `SignalizationState`: 1 Red, 2 RedAmber, 3 Green, 4 Amber, 5 Off, 6 Undefined, 7 FlashingAmber, 8 FlashingRed, 9 FlashingGreen, 10 AlternatingRedGreen, 11 GreenAmber; EX satr bilan yozadi: `"GREEN"`, `"RED"`, `"AMBER"`, `"REDAMBER"` |
| `ContrByCOM` | SignalGroup | `vissim_set_signal_state` | AX, EX | documented | `SigState` yozilganda avtomatik `True`; `False` — kontrollerga qaytarish; faqat simulyatsiya paytida faol |

## Sozlanadigan qiymatlar (PLAN.md 5-bo'lim, (*) belgilari)

| O'zgaruvchi | Standart | Asos | Holat |
|---|---|---|---|
| `VISSIM_SEED_ATTR` | `RandSeed` | AX, EX | configurable (documented) |
| `VISSIM_BREAK_DISABLE_VALUE` | `0` | EX: "`SimBreakAt` = 0 means no break!", VBS: "If set to 0 then the simulation is not paused"; AX: min 0, standart 0 | configurable (documented) |

## M6 da tekshiriladigan jarayon xatti-harakatlari

| Nima | Hozirgi holat (M2) | M6 da tekshirish |
|---|---|---|
| Job Object `LimitFlags` | Faqat `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (0x2000), PLAN 6.4 bo'yicha; `BREAKAWAY_OK` yo'q | Haqiqiy Vissim (litsenziya yordamchisi, crash reporter, yordam oynasi va h.k.) `CREATE_BREAKAWAY_FROM_JOB` bilan jarayon ochadimi? Ochsa, Windows uni rad etadi (`ERROR_ACCESS_DENIED`). Shunday bo'lsa — flag'ni o'zgartirish faqat loyiha egasining roziligi bilan |
| `Exit()` javobi | Javob qaytsa ham, `RPC_S_CALL_FAILED` / `RPC_E_DISCONNECTED` bilan tugasa ham jarayon 10 s kutiladi | Haqiqiy Vissim `Exit()` da qanday javob beradi va jarayon qancha vaqtda tugaydi |
| `ItemByKey` kalit turi | `vissim_set_attribute_bulk` kalitni o'zgartirmasdan uzatadi; `5` va `"5"` takror kalit sifatida rad etiladi. Soxta (fake) kolleksiya satr kalitni butun songa aylantiradi — bu **tasdiqlanmagan** | Butun sonli kalitli kolleksiyada (`Net.Links`) `ItemByKey("5")` ishlaydimi, xato beradimi yoki boshqa obyektni qaytaradimi. Ishlamasa — raqamli satr kalitni `int` ga aylantirish yoki rad etish |

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
