# Test-Spec: CouchDB-Layer, Smart-Lists Tools, Habit-Streaks

Status: Planungsphase. Diese Spec definiert WAS getestet wird, nicht WIE.
Adressaten sind der Implementierer und nachgelagert der Test-Runner.

## Konventionen aus dem Repo (gelten fuer alle drei Familien)

- Pfad: `tests/test_api.py` ist Vorbild. Neue Tests koennen entweder dort als
  weitere Klassen leben oder als eigene Dateien `tests/test_couchdb.py`,
  `tests/test_smartlists.py`, `tests/test_habit_streaks.py`. Empfehlung:
  separate Dateien, weil `test_api.py` schon ueberladen ist.
- Live-Test-Pattern: Modul-Level Fixture `api_client` aus `tests/test_api.py`
  uebernehmen — sie ruft `pytest.skip("No API key available for testing")`
  wenn `AMAZING_MARVIN_API_KEY` fehlt. Fuer CouchDB-Live-Tests analog eine
  Fixture `couchdb_client`, die zusaetzlich auf `has_couchdb` prueft und
  sonst skipt.
- Bekannter Bug (NICHT nachbauen): `TestProjectPlanningEnhancements` ruft
  `create_api_client()` direkt ohne Skip-Pattern. Wenn der API-Key fehlt,
  schlaegt der Test mit Pydantic-Validation-Error fehl statt zu skippen.
  Neue Live-Tests MUESSEN das `api_client`-Fixture-Pattern nutzen.
- Mocking-Stil: `unittest.mock.patch` mit `requests.post` / `requests.get`.
  Im Repo wird kein `requests_mock` verwendet, also bei der bestehenden
  Konvention bleiben.
- Async-Tools (MCP-Tools): mit `asyncio.run(tool(...))` aufrufen, siehe
  z.B. `TestDeleteDocumentTool`.

---

## A) CouchDB-Layer (`MarvinAPIClient.find_docs`, `has_couchdb`)

### Implementierungsbezogene Hinweise (Testbarkeit)

- `has_couchdb` muss ein Property sein, das ALLE vier db_*-Settings prueft,
  nicht nur Truthy auf einem. Sonst lassen sich die "partial config"-Edge-Cases
  nicht praezise testen.
- Die URL-Konstruktion fuer `/_find` sollte aus einer Hilfsfunktion kommen,
  damit die URL-encoded-Password-Behandlung isoliert testbar ist (Basic-Auth
  via `requests.auth.HTTPBasicAuth` ist sauberer als manuelles Header-Setzen
  und macht den Test schmaler).
- `find_docs` sollte den Selector-Dict NICHT mutieren (Test prueft Idempotenz
  auf dem Caller-Dict).
- `limit` sollte als top-level-Key in das POST-Body gehen, nicht in den
  Selector — sonst kann CouchDB ihn ignorieren.

### A.1 `has_couchdb` property

- [Unit] `has_couchdb_true_when_all_four_settings_present` — alle vier
  Settings gesetzt -> True.
- [Unit] `has_couchdb_false_when_uri_missing` — DB_URI=None -> False.
- [Unit] `has_couchdb_false_when_name_missing` — DB_NAME=None -> False.
- [Unit] `has_couchdb_false_when_user_missing` — DB_USER=None -> False.
- [Unit] `has_couchdb_false_when_password_missing` — DB_PASSWORD=None ->
  False.
- [Unit] `has_couchdb_false_when_setting_is_empty_string` — leerer String
  zaehlt wie None (analog zum `has_full_access`-Pattern in `api.py`, das
  `bool(...)` nutzt).
- [Unit] `has_couchdb_false_when_no_settings_present` — kein db_*-Setting
  ueberhaupt -> False.

### A.2 `find_docs(selector, fields=None, limit=500)` — Guard + Request-Aufbau

- [Unit] `find_docs_raises_value_error_when_not_configured` — Aufruf ohne
  CouchDB-Config wirft `ValueError`. Message muss die ENV-Variablen-Praefixe
  nennen (mind. `AMAZING_MARVIN_DB_URI`), damit der Caller weiss, was zu
  setzen ist. Analog zum bestehenden `AMAZING_MARVIN_FULL_ACCESS_TOKEN`-Guard.
- [Unit] `find_docs_posts_to_correct_url` — `requests.post` wird mit
  `{DB_URI}/{DB_NAME}/_find` aufgerufen (Trailing-Slash-Variante explizit
  testen: URI ohne und mit Trailing-Slash darf nicht zu Doppelslash fuehren).
- [Unit] `find_docs_sends_basic_auth` — der `auth`-Parameter (oder
  Authorization-Header) wird mit User+Password gesetzt.
- [Unit] `find_docs_sends_selector_in_body` — POST-Body enthaelt
  `{"selector": <input>, "limit": 500}`.
- [Unit] `find_docs_with_fields_includes_fields_key` — `fields=["_id","name"]`
  landet als `fields`-Key im Body.
- [Unit] `find_docs_without_fields_omits_fields_key` — `fields=None` -> KEIN
  `fields`-Key im Body (CouchDB interpretiert leere Liste als "nichts
  zurueckgeben").
- [Unit] `find_docs_custom_limit_overrides_default` — `limit=10` landet im
  Body, nicht 500.
- [Unit] `find_docs_returns_docs_array` — Response `{"docs": [...]}` wird
  ausgepackt; Returnwert ist die Liste, nicht das Wrapper-Objekt.
- [Unit] `find_docs_returns_empty_list_when_no_docs` — Response
  `{"docs": []}` -> `[]` (kein Crash, kein None).
- [Unit] `find_docs_does_not_mutate_selector_arg` — der vom Caller uebergebene
  Selector-Dict ist nach dem Aufruf unveraendert (Test: Identitaet plus
  Inhalt).

### A.3 `find_docs` — Fehlerpfade

- [Unit] `find_docs_raises_on_http_401` — `requests.post` Mock liefert 401,
  `raise_for_status` propagiert HTTPError; der Caller bekommt klar
  `requests.exceptions.HTTPError` (nicht maskiert).
- [Unit] `find_docs_raises_on_http_500` — analog 500.
- [Unit] `find_docs_raises_on_connection_error` — `requests.post`
  side_effect=ConnectionError -> propagiert.
- [Unit] `find_docs_logs_but_does_not_swallow_errors` — wie im
  `_make_request`-Pattern (`logger.exception` + raise).

### A.4 Hinweis: Selector OHNE `db`-Key — Boundary

Frage aus dem Auftrag: "selector ohne `db` (sollte limit erzwingen?)".

Empfehlung: **Nicht** in `find_docs` selbst durchsetzen. `find_docs` ist
generisch. Schutzlogik gehoert in die Smart-List-Tools (die nutzen IMMER
`{"db":"SmartLists"}`). Ein zusaetzlicher Test ist trotzdem sinnvoll:

- [Unit] `find_docs_passes_through_db_less_selector` — Selector ohne `db`-Key
  wird unveraendert weitergereicht. Begruendung: dokumentiert die
  Verantwortlichkeit. Keine Magie an dieser Stelle.

### A.5 Sonderzeichen im Passwort

Wenn Basic-Auth via `requests.auth.HTTPBasicAuth(user, password)` gemacht
wird, kuemmert sich `requests` um Encoding. Wenn die Implementierung jedoch
das Passwort in die URL einbaut (NICHT empfohlen), muss URL-Encoding getestet
werden.

- [Unit] `find_docs_password_with_special_chars_handled` — Passwort
  `p@ss/wo:rd!` wird korrekt uebergeben. Test prueft entweder den
  `auth`-Parameter (bevorzugt) oder dass der Authorization-Header
  base64-encoded ist und beim Decoden das Originalpasswort enthaelt.

### A.6 Live-Integration (CouchDB)

- [Live] `find_docs_smartlists_returns_list` — Selector
  `{"db":"SmartLists"}` gegen echtes CouchDB; Returnwert ist eine Liste.
  Skipt wenn `has_couchdb` False.
- [Live] `find_docs_unknown_db_returns_empty` — Selector
  `{"db":"NonExistentDb_xyz"}` -> leere Liste, kein Fehler.
- [Live] `find_docs_limit_param_respected` — Selector `{"db":"SmartLists"}`
  mit `limit=1` -> hoechstens 1 Doc.

### A.7 Was NICHT getestet wird

- CouchDB-Query-Syntax (Mango-Selectors): Verantwortung des Aufrufers.
- Mehrseitige `_find`-Pagination ueber `bookmark`: nicht im Scope dieses
  PRs; >500 Docs werden bewusst nicht gehandhabt. Implementierungs-Notiz:
  wenn ein Caller mehr braucht, expliziter neuer PR.
- Live-CouchDB-Status (Cluster-Errors, Quorum): Infrastruktur, nicht unser
  Code.

### A.8 Mocking-Strategie A

- Unit: `@patch("requests.post")` auf das Modul, in dem `find_docs` lebt
  (also `amazing_marvin_mcp.api.requests.post` falls importiert als
  `import requests`, sonst der genaue Importpfad).
- `_mock_response`-Helper aus `tests/test_api.py` wiederverwenden (status,
  json, raise_for_status).
- `has_couchdb` Tests: kein HTTP noetig — Settings direkt im Client-Konstruktor
  setzen (oder Settings-Mock via `patch("amazing_marvin_mcp.api.get_settings")`,
  je nachdem wo die Werte gelesen werden).

---

## B) Smart-Lists Tools (`smartlists.py`)

### Implementierungsbezogene Hinweise (Testbarkeit)

- Die Whitelist der erlaubten Felder (`name`, `groupBy`, `sort`, `refill`,
  `limit`, `oneRT`, `removeRedundancies`, `fieldUpdates`, sowie die
  Filter-Top-Level-Felder `itemType`, `recurring`, `parentId`, `goalId`,
  `title`, `hasTime`, `day`, `dueDate`, `labelIds`, `project`, `planAhead`,
  `advanced`, ...) sollte als Modul-Konstante in `smartlists.py` exportiert
  werden. Tests koennen darauf zugreifen statt sie zu duplizieren.
- Die Mapping-Funktion `dict -> setters` (fuer Update) sollte als pure
  Hilfsfunktion existieren (`_to_setters(changes)`), nicht im Tool inline.
  Test-Hinweis: pure Funktion -> trivial testbar.
- Filter-Clauses sind dicts mit `op` und optional `val`. Die Validierung
  sollte sein: "Key gehoert zur Filter-Whitelist UND value ist entweder None
  oder ein dict mit `op`-Key". KEINE tiefere Validierung des `op`-Strings
  (das ist Marvin-API-Territorium).
- `delete_smart_list` MUSS zuerst `get_document` aufrufen und `db ==
  "SmartLists"` pruefen, BEVOR `delete_document` aufgerufen wird. Das ist
  die Sicherheits-Garantie und MUSS testbar isoliert sein (Test:
  delete_document darf nicht aufgerufen werden wenn `db != "SmartLists"`).

### B.1 `list_smart_lists()`

- [Unit] `list_smart_lists_requires_couchdb` — wenn `has_couchdb` False,
  liefert das Tool eine Error-Response (kein Crash). Genauer: das Tool
  prueft das Property selbst oder `find_docs` wirft `ValueError`, die in
  eine `StandardResponse(success=False)` umgesetzt wird. Welche Variante
  gewaehlt wird, entscheidet die Implementierung — Test prueft das
  Endverhalten (success=False, klare Message mit ENV-Variablen-Namen).
- [Unit] `list_smart_lists_calls_find_docs_with_smartlists_selector` —
  `find_docs` wird mit `{"db":"SmartLists"}` aufgerufen.
- [Unit] `list_smart_lists_returns_projection` — Tool transformiert jeden
  Doc auf `{id, name, sort, limit, ...}` (Felder die der Test-Architekt
  spezifiziert — siehe Implementierungs-Code). Test prueft mind., dass
  `_id` als `id` umbenannt wird und dass irrelevante Felder (z.B.
  `_rev`, `fieldUpdates`) NICHT durchgereicht werden.
- [Unit] `list_smart_lists_empty_result` — `find_docs` -> `[]`, Tool
  liefert success=True mit leerer Liste.
- [Live] `list_smart_lists_live` — gegen echte CouchDB, Returnwert ist Liste.

### B.2 `get_smart_list(smart_list_id)`

- [Unit] `get_smart_list_calls_get_document` — `api.get_document(id)` wird
  mit der ID aufgerufen.
- [Unit] `get_smart_list_returns_full_doc` — Returnwert enthaelt das
  unveraenderte Doc (keine Whitelist beim READ, weil read-only).
- [Unit] `get_smart_list_propagates_404` — `get_document` raises HTTPError
  mit response.status_code=404 -> Tool liefert success=False, nicht crash.
- [Unit] `get_smart_list_wrong_doc_type_warning` — `get_document` liefert
  einen Doc mit `db != "SmartLists"`. Empfehlung: Tool soll trotzdem den
  Doc zurueckgeben (kein Hard-Fail beim Read), aber im Response-Summary
  warnen. Alternativ: Hard-Fail wie bei delete. Welche Variante gewaehlt
  wird, MUSS der Test fixieren. Vorschlag: Hard-Fail mit success=False,
  weil sonst der Caller unbemerkt einen Habit-Doc als "Smart-List"
  behandeln koennte.

### B.3 `create_smart_list(name, sort=None, filter_clauses=None, ...)`

- [Unit] `create_smart_list_minimal_args` — nur `name` -> Tool ruft
  `api.create_document({"db":"SmartLists", "name":"X", "createdAt":..., "updatedAt":...})`.
  `db`-Key MUSS gesetzt sein. `createdAt`/`updatedAt` muessen ms-epoch sein
  (Test mit `patch("time.time")` analog zu `TestSettersBuilder`).
- [Unit] `create_smart_list_with_sort` — `sort=[{"field":"day","dir":"asc"}]`
  -> landet 1:1 im Doc.
- [Unit] `create_smart_list_with_filter_clauses_simple` —
  `filter_clauses={"itemType": {"op":"task"}}` -> landet als top-level
  `itemType`-Key im Doc, nicht als nested `filter`-Liste. Begruendung:
  Marvin-Smart-Lists nutzen per-clause top-level Felder (vom User
  empirisch ermittelt).
- [Unit] `create_smart_list_filter_clause_without_val` —
  `{"hasTime": {"op":"task"}}` (op ohne val) -> wird akzeptiert und
  unveraendert durchgereicht.
- [Unit] `create_smart_list_filter_clause_advanced_y_op` —
  `{"advanced": {"op":"y", "val":"<RPN-string>"}}` -> wird akzeptiert,
  der RPN-String NICHT geparst/validiert.
- [Unit] `create_smart_list_filter_clause_thisWeek_macro` —
  `{"day": {"op":"&thisWeek"}}` -> akzeptiert, `&`-Praefix bleibt erhalten.
- [Unit] `create_smart_list_filter_clause_in_with_uuid` —
  `{"parentId": {"op":"in", "val":"uuid-here"}}` -> 1:1 durchgereicht.
- [Unit] `create_smart_list_rejects_non_whitelisted_field` — Caller
  uebergibt `filter_clauses={"evilField": {"op":"x"}}` -> Tool liefert
  success=False, kein API-Call. Whitelist-Violation MUSS klar in der
  Summary-Message stehen (Feldname genannt).
- [Unit] `create_smart_list_rejects_non_dict_clause` —
  `filter_clauses={"itemType": "task"}` (kein dict) -> success=False.
- [Unit] `create_smart_list_rejects_clause_without_op` —
  `filter_clauses={"itemType": {"val":"x"}}` (kein `op`) -> success=False.
- [Unit] `create_smart_list_includes_all_whitelisted_top_level_fields` —
  `refill=True, oneRT=False, removeRedundancies=True, limit=20` -> alle im
  Doc.
- [Unit] `create_smart_list_propagates_api_error` — `create_document` wirft
  HTTPError -> success=False.

### B.4 `update_smart_list(smart_list_id, **changes)`

- [Unit] `update_smart_list_calls_update_document_with_setters` — `changes
  = {"name":"New"}` -> `update_document(id, setters=[...])` mit
  `name`-Setter UND `updatedAt`-Setter (analog
  `TestSettersBuilder.test_title_only_produces_title_and_updatedAt`).
- [Unit] `update_smart_list_empty_changes_still_bumps_updatedAt` — keine
  Aenderungen -> Setter enthaelt NUR `updatedAt`. Begruendung: konsistent
  mit `build_setters([])` aus dem bestehenden Pattern.
- [Unit] `update_smart_list_filter_clauses_become_top_level_setters` —
  `filter_clauses={"day":{"op":"&thisWeek"}}` -> Setter mit `key="day"` und
  `val={"op":"&thisWeek"}`. KEIN nested `filter.day`-Pfad.
- [Unit] `update_smart_list_clear_filter_clause` —
  `filter_clauses={"day": None}` -> Setter `{"key":"day","val":null}`
  (Loeschen einer Clause).
- [Unit] `update_smart_list_rejects_non_whitelisted_field` — `evilField=1`
  -> success=False, kein API-Call.
- [Unit] `update_smart_list_404` — `update_document` wirft HTTPError 404 ->
  success=False mit klarer Message.

### B.5 `delete_smart_list(smart_list_id)`

- [Unit] `delete_smart_list_reads_doc_first` — Tool ruft `get_document(id)`
  vor `delete_document`. Test prueft Call-Order via Mock-`mock_calls`.
- [Unit] `delete_smart_list_happy_path` — `get_document` -> `{"_id":"sl1",
  "db":"SmartLists", "name":"X"}` -> `delete_document("sl1")` wird
  aufgerufen, success=True, Summary enthaelt Name.
- [Unit] `delete_smart_list_safety_blocks_wrong_db` — `get_document` ->
  `{"_id":"h1", "db":"Habits"}` -> `delete_document` wird NICHT aufgerufen,
  success=False, Summary nennt die tatsaechliche `db`. Diese Garantie ist
  der Kern des Tools.
- [Unit] `delete_smart_list_safety_blocks_missing_db_field` — `get_document`
  -> Doc ohne `db`-Key (z.B. malformed) -> Block, success=False. Begruendung:
  defense in depth, `KeyError` waere unfreundlich.
- [Unit] `delete_smart_list_404_on_read` — `get_document` raises HTTPError
  404 -> success=False, `delete_document` wird NICHT aufgerufen.
- [Unit] `delete_smart_list_propagates_delete_error` — Read ok, aber
  `delete_document` schlaegt fehl -> success=False.

### B.6 Was NICHT getestet wird

- Marvin-interne Validierung der Smart-List-Doc-Struktur (das macht der
  Server).
- Grammatik des `advanced` op-RPN-Strings: undokumentiert, nicht stabil,
  ausserhalb unseres Scope.
- System-Smart-Lists (Today/Backlog/Tomorrow/...): existieren nicht in
  CouchDB, koennen nicht erstellt/gelesen/gemodifiziert werden.
- CRDT-Merge-Semantik der `updatedAt`-Stempel.
- JSON-Schema-Validation der `sort`-Liste (`field`, `dir`-Werte).

### B.7 Mocking-Strategie B

- Pro Tool: `@patch("amazing_marvin_mcp.smartlists.create_api_client")`
  (Pattern aus `TestDeleteDocumentTool`).
- Client = `MagicMock(spec=MarvinAPIClient)`.
- Methoden setzen: `client.find_docs.return_value`,
  `client.get_document.return_value`, `client.create_document.return_value`,
  `client.update_document.return_value`, `client.delete_document.return_value`.
- Fuer `updatedAt`/`createdAt`-Assertions: `@patch("...smartlists.time.time")`
  oder `@patch("...smartlists.DateUtils...")` — je nach Implementierung.

---

## C) Habit Streak Calculation (`habits.py` oder `streaks.py`)

### Implementierungsbezogene Hinweise (Testbarkeit)

- Die Streak-Berechnung MUSS aufgeteilt werden in:
  1. `_parse_history(history_flat_list) -> list[(ts_ms, value)]` (pure)
  2. `_bucket_by_period(entries, period, tz) -> dict[bucket_key, list[value]]`
     (pure)
  3. `_compute_streak(buckets_sorted, target, today_bucket) -> (current,
     longest)` (pure)
  4. Tool-Wrapper, der `api.get_document(habit_id)` aufruft und I/O macht.
  
  Begruendung: nur so sind die Edge-Cases mit Unit-Tests ohne Mock
  durchspielbar. Wenn alles in einer grossen Funktion lebt, brauchen wir
  Integrationstests um TZ-Boundaries zu pruefen — das ist zu teuer und
  fragil.
- "Heute" via `DateUtils.get_today()` injectable (Test patcht das Modul,
  Pattern aus `TestTimezoneAwareness`).
- TZ-Konvertierung: ms-epoch -> lokale `datetime` (Pythons
  `datetime.fromtimestamp(ts/1000)` ohne `tz`-Arg gibt lokale Zeit). Das
  ist die richtige Boundary, weil "Heute" in Marvin lokal definiert ist.

### C.1 `get_habit_streak(habit_id, target_per_period=None)` — Tool-Verhalten

- [Unit] `get_habit_streak_calls_get_document_with_habit_id` —
  `api.get_document(habit_id)` wird aufgerufen.
- [Unit] `get_habit_streak_returns_current_and_longest` — Response enthaelt
  `current_streak` (int) und `longest_streak` (int).
- [Unit] `get_habit_streak_returns_period_used` — Response enthaelt
  `period` ("day"|"week"|"month") aus dem Doc, damit der Caller den Kontext
  sieht.
- [Unit] `get_habit_streak_returns_target_used` — Response zeigt das
  effektiv genutzte Target (aus Arg oder default 1).
- [Unit] `get_habit_streak_propagates_404` — `get_document` wirft 404 ->
  success=False.
- [Unit] `get_habit_streak_handles_missing_history_field` — Doc ohne
  `history`-Key -> beide Streaks 0, success=True. Begruendung: neu
  erstellte Habits haben keine Historie.

### C.2 `_parse_history` — Pure Parsing

- [Unit] `parse_history_empty_list` — `[]` -> `[]`.
- [Unit] `parse_history_single_pair` — `[1700000000000, 1]` -> `[(1700000000000, 1)]`.
- [Unit] `parse_history_multiple_pairs` — `[t1,v1,t2,v2,t3,v3]` -> drei
  Tupel.
- [Unit] `parse_history_odd_length_raises_or_drops_tail` — `[t1, v1, t2]`
  (ungerade Laenge, malformed). Empfehlung: ValueError raisen, NICHT
  silent droppen — sonst maskieren wir Datenkorruption. Test fixiert das
  Verhalten.
- [Unit] `parse_history_value_can_be_float` — `[t, 1.5]` -> akzeptiert
  (recordType=number Habits koennen Floats haben? — siehe Open Question
  unten).

### C.3 `_bucket_by_period` — Pure Bucketing

- [Unit] `bucket_period_day_groups_per_day` — drei Eintraege am selben Tag
  -> 1 Bucket mit 3 Values.
- [Unit] `bucket_period_day_separates_days` — Eintraege an 3 verschiedenen
  Tagen -> 3 Buckets.
- [Unit] `bucket_period_week_iso_week_grouping` — Eintraege am Sonntag und
  Montag derselben ISO-Woche -> 1 Bucket. Eintraege Sa/So Wochengrenze ->
  2 Buckets. Begruendung: ISO-Wochen starten Montag, das ist die Marvin-
  Konvention (zu pruefen — siehe Open Question).
- [Unit] `bucket_period_month_groups_per_calendar_month` — 28. Jan und 3.
  Feb -> 2 Buckets. Beide am 31. Jan -> 1 Bucket.
- [Unit] `bucket_history_unsorted_input_produces_correct_buckets` —
  Eingabe in Zufallsreihenfolge -> Output ist deterministisch korrekt
  gruppiert. Begruendung vom User: `history` ist NICHT sortiert, das ist
  der Hauptpunkt.
- [Unit] `bucket_local_tz_boundary_23_utc_is_next_day_local` — Eintrag mit
  ts = 23:00 UTC am 14.04. landet bei lokaler TZ Europe/Berlin (UTC+2)
  im Bucket fuer 15.04. Test setzt explizit eine TZ via
  `patch("...habits.LOCAL_TZ", ...)` oder per `os.environ["TZ"]` + Reset.
  Begruendung vom User: TZ-Boundary ist der haeufigste Streak-Fehler.
- [Unit] `bucket_local_tz_boundary_01_utc_is_same_day_local` — Eintrag
  01:00 UTC am 15.04., lokale TZ UTC-5 (z.B. America/New_York) -> Bucket
  fuer 14.04. Spiegelfall.

### C.4 `_compute_streak` — Pure Streak-Logik

- [Unit] `streak_empty_buckets` — keine Buckets -> (0, 0).
- [Unit] `streak_single_bucket_today_meets_target` — 1 Bucket = today mit
  value >= target -> (1, 1).
- [Unit] `streak_single_bucket_today_fails_target` — 1 Bucket = today mit
  value < target -> (0, 0).
- [Unit] `streak_today_not_yet_recorded_does_not_break_streak` — letzter
  Eintrag war gestern (target erreicht), heute noch leer. Erwartung:
  `current_streak` zeigt den gestrigen Streak weiter, weil "der Tag ist
  noch nicht vorbei". Begruendung: User-Hinweis "streak laeuft heute aus?".
  Test fixiert: KEIN Reset auf 0 nur weil heute noch leer ist.
- [Unit] `streak_gap_yesterday_resets_current_streak` — letzter Eintrag
  vorgestern (target erreicht), gestern und heute leer -> current_streak=0,
  longest_streak >= 1.
- [Unit] `streak_consecutive_days_increments` — drei Tage in Folge mit
  target erreicht -> (3, 3).
- [Unit] `streak_longest_preserved_across_break` — Muster: 5 Tage erfuellt,
  1 Tag Pause, 2 Tage erfuellt (inkl. heute) -> (current=2, longest=5).
- [Unit] `streak_boolean_recordtype_sum_equals_count` —
  recordType="boolean", target=1, buckets mit `[1]` jeweils -> Streak
  zaehlt richtig. Mit `[0]` -> kein Streak.
- [Unit] `streak_number_recordtype_partial_fulfillment` —
  recordType="number", target_per_period=3, bucket=`[1, 1]` (Summe=2) ->
  Tag zaehlt NICHT als erfuellt, Streak bricht.
- [Unit] `streak_number_recordtype_overfulfillment` — bucket=`[5]`,
  target=3 -> erfuellt.
- [Unit] `streak_number_recordtype_multiple_partial_sum_to_target` —
  bucket=`[1, 1, 1]`, target=3 -> erfuellt (Summe == target).
- [Unit] `streak_period_week_with_partial_current_week` — Bucket fuer
  diese Woche enthaelt nur Mittwoch-Eintrag, target erreicht. Bucket
  fuer letzte Woche ebenfalls erreicht. -> current_streak=2 (current week
  zaehlt mit, weil target SCHON erreicht — auch wenn Woche noch nicht
  vorbei).
- [Unit] `streak_period_week_current_week_not_yet_meeting_target` — Bucket
  current week hat noch nicht genug; letzte Woche erfuellt. Wie behandeln?
  Empfehlung: Spiegelung der Day-Logik: aktueller (unvollstaendiger)
  Period-Bucket zaehlt NICHT als "Bruch", nur als "noch nicht erfuellt".
  current_streak bleibt auf dem Wert der letzten erfuellten Periode.
  Begruendung vom User: "period='week' mit unvollstaendiger aktueller
  Woche".
- [Unit] `streak_target_default_is_one` — `target_per_period=None` -> Tool
  nutzt 1 als Default fuer boolean-Habits, sodass jeder Eintrag erfuellt.
- [Unit] `streak_target_override_wins_over_doc_default` — Habit-Doc hat
  ein internes Target (falls vorhanden), Arg ueberschreibt. Wenn das Doc
  keinen Target-Key hat: Arg nutzen, sonst 1.

### C.5 Integrationstests Habits

- [Live] `get_habit_streak_live_existing_habit` — gegen echte API mit
  einer bekannten Habit-ID (aus `.env` z.B. `MARVIN_TEST_HABIT_ID`).
  Skipt wenn Variable nicht gesetzt. Test prueft nur, dass beide Streak-Werte
  ints sind und current_streak <= longest_streak.

### C.6 Was NICHT getestet wird

- Marvin's eigene Streak-Anzeige (sollte zwar matchen, ist aber nicht
  unsere Source of Truth; Wenn ein Test gegen Marvin-UI ginge, waere das
  fragil).
- Beliebige Custom-Periods (z.B. "every 3 days") — nicht im Datenmodell
  laut User.
- Habit `recurring`-Logik (das ist eine andere Property, nicht Streak).
- Was passiert wenn `period` ein unbekannter Wert ist (z.B. `"year"`) —
  Open Question, siehe unten. Vorschlag: ValueError, aber explizit testen
  sobald Verhalten festgelegt ist.

### C.7 Mocking-Strategie C

- Unit-Tests fuer `_parse_history`, `_bucket_by_period`, `_compute_streak`:
  KEIN Mock, pure Funktionsaufrufe.
- TZ-Tests: zwei Optionen, beide akzeptabel:
  a) `monkeypatch.setenv("TZ", "Europe/Berlin")` + `time.tzset()` (POSIX
     only, was hier ok ist).
  b) Bucketing nimmt expliziten `tz`-Parameter (zoneinfo.ZoneInfo), Tests
     injizieren.
  Empfehlung: (b), weil Windows-tauglich und kein globaler State.
- Tool-Wrapper-Tests: `@patch("amazing_marvin_mcp.habits.create_api_client")`
  + `client.get_document.return_value` mit handgebautem Habit-Doc.
- "Heute"-Tests: `@patch("amazing_marvin_mcp.habits.DateUtils.get_today",
  return_value="2026-04-14")` analog zu `TestTimezoneAwareness`.

---

## Open Questions

1. **CouchDB-Settings-Quelle**: Werden die vier `AMAZING_MARVIN_DB_*`
   Variablen ueber `pydantic_settings` in `Settings` ergaenzt? Wenn ja, gilt
   automatisch das `env_file = ".env"`-Pattern. Falls eine separate
   `CouchDBSettings`-Klasse gewuenscht ist, weichen die Mocks ab.

2. **`has_couchdb` Live-Skip**: Sollen Live-Tests einzeln skippen oder als
   Klassen-Level-Skip ueber ein `pytestmark`? Vorschlag: Fixture
   `couchdb_client` mit Skip drin, dann tut sich der Implementierer
   nichts mehr.

3. **`get_smart_list` bei falschem `db`**: Hard-Fail (success=False) oder
   weiches Warning? Spec geht von Hard-Fail aus. Bestaetigung erwuenscht.

4. **`recordType="number"` mit Float-Values**: Sind Float-Eingaben in der
   `history` realistisch oder immer int? Auswirkung auf
   `parse_history_value_can_be_float`. Bitte vor Implementierung anhand
   echter Habit-Docs gegenchecken.

5. **Wochen-Boundary**: Marvin Smart-Lists nutzen `&thisWeek` etc. Welcher
   Wochenstart? ISO (Montag) oder Sonntag? Wichtig fuer
   `bucket_period_week_iso_week_grouping`. Vorschlag: per ZoneInfo +
   `datetime.isocalendar()` (Montag-Start). Wenn das nicht zu Marvin
   passt, muss der Test angepasst werden.

6. **Unbekanntes `period` im Habit-Doc**: ValueError vs. Fallback auf
   "day". Bisher nur in "Was NICHT getestet" erwaehnt — wenn das
   Verhalten festgelegt wird, gehoert ein Test dazu.

7. **>500 Docs**: Hartes 500-Limit ohne Pagination ist okay fuer den
   Smart-Lists-Use-Case (Anwender haben selten >500 Smart-Lists). Wenn
   `find_docs` aber spaeter fuer andere DBs (Tasks!) wiederverwendet
   wird, ist Pagination Pflicht. Sollte als Code-Kommentar in `find_docs`
   stehen — und als Open Question hier festgehalten sein. Kein Test
   bisher, weil bewusster Scope-Cut.

8. **`AMAZING_MARVIN_DB_USER` darf leer sein?**: Manche CouchDB-Setups
   nutzen "anonymous" oder keinen User. Hier wird `has_couchdb=False`
   sein. Ok? Wenn ja: dokumentieren. Wenn nein: separate Codepfade.
