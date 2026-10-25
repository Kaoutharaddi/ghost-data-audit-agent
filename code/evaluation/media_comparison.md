# Media extraction: API run vs hand-written cache

- API file: `code/data/media_extractions.api.json`
- Cache file: `code/data/media_extractions.json`

| Matched records | Missing in API | Extra in API | Files not extracted | Field agreement |
|---|---|---|---|---|
| 13 | 3 | 12 | 0 | 84.6% |

## Differences

| File | Resource | Field / issue | Cache | API |
|---|---|---|---|---|
| account_admin_console_20260926.png | support_9538 | extra in API output |  |  |
| account_admin_console_20260926.png | account_5762 | status | "" | "IDENTIFIABLE" |
| account_history_report_20260718.pdf | export_1842 | missing in API output |  |  |
| backup_restore_console_20260429.png | backup_9286 | extra in API output |  |  |
| backup_restore_console_20260429.png | backup_9288 | extra in API output |  |  |
| backup_restore_console_20260429.png | backup_9291 | extra in API output |  |  |
| backup_restore_console_20260429.png | backup_9287 | status | "" | "IDENTIFIABLE" |
| backup_restore_console_20260429.png | backup_9287 | restore_allowed | true | null |
| connector_management_console_20260807.png | data_flow_1121 | status | "" | "IDENTIFIABLE" |
| connector_management_console_20260807.png | data_flow_9846 | status | "" | "IDENTIFIABLE" |
| customer_export_console_20260618.png | export_1258 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1259 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1261 | extra in API output |  |  |
| customer_export_console_20260618.png | export_1260 | status | "" | "IDENTIFIABLE" |
| deleted_account_reference_20260310.png | tombstone_17 | missing in API output |  |  |
| delivery_correction_attachment.pdf | support_001 | category | "SUPPORT_ATTACHMENT" | "SUPPORT_MESSAGE" |
| delivery_correction_attachment.pdf | support_001 | status | "" | "IDENTIFIABLE" |
| marketing_profile_console.png | marketing_001 | status | "" | "IDENTIFIABLE" |
| marketing_profile_console.png | marketing_001 | keys | [] | ["sj.park@naver.com"] |
| object_storage_browser_20260213.png | object_19 | missing in API output |  |  |
| search_index_inspector_20260901.png | search_1874 | status | "" | "IDENTIFIABLE" |
| search_index_inspector_20260901.png | search_428 | status | "" | "IDENTIFIABLE" |
| security_audit_explorer_20260905.png | audit_record_001 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_002 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_004 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_005 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_006 | extra in API output |  |  |
| security_audit_explorer_20260905.png | audit_record_003 | status | "" | "IDENTIFIABLE" |
| security_audit_explorer_20260905.png | audit_record_003 | pii | {} | {"user_ref": "user_03"} |
| security_retention_extract.pdf | audit_record_003 | status | "" | "IDENTIFIABLE" |
| security_retention_extract.pdf | audit_record_003 | pii | {} | {"user_ref": "user_03"} |

## Impact on the agent

Evaluation cases whose output changes: **6** (case_33, case_34, case_45, case_46, case_55, case_61)

| Public samples | status acc. | step F1 | sequence acc. |
|---|---|---|---|
| with cache | 1.000 | 0.971 | 0.688 |
| with API extraction | 1.000 | 0.955 | 0.688 |
