# Media extraction: API run vs hand-written cache

- API file: `code/data/media_extractions.api_v2.json`
- Cache file: `code/data/media_extractions.json`

| Matched records | Missing in API | Extra in API | Files not extracted | Field agreement |
|---|---|---|---|---|
| 13 | 3 | 11 | 0 | 97.1% |

## Differences

| File | Resource | Field / issue | Cache | API |
|---|---|---|---|---|
| account_history_report_20260718.pdf | export_1842 | missing in API output |  |  |
| backup_restore_console_20260429.png | backup_9286 | extra in API output |  |  |
| backup_restore_console_20260429.png | backup_9288 | extra in API output |  |  |
| backup_restore_console_20260429.png | backup_9291 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1258 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1259 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1261 | extra in API output |  |  |
| deleted_account_reference_20260310.png | tombstone_17 | missing in API output |  |  |
| marketing_profile_console.png | marketing_001 | keys | [] | ["sj.park@naver.com"] |
| object_storage_browser_20260213.png | object_19 | missing in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_001 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_002 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_004 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_005 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_006 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_003 | pii | {} | {"user_ref": "user_03"} |
| security_retention_extract.pdf | audit_record_003 | pii | {} | {"user_ref": "user_03"} |

## Impact on the agent

Evaluation cases whose output changes: **1** (case_45)

| Public samples | status acc. | step F1 | sequence acc. |
|---|---|---|---|
| with cache | 1.000 | 0.990 | 0.875 |
| with API extraction | 1.000 | 0.990 | 0.875 |
