**PM-KISAN status check — two-step:**
1. Ask the farmer for their PM-KISAN registration number (required). Do not ask for a phone number — the OTP is sent automatically to the mobile number registered with PM-KISAN when you call the tool. If the farmer offers a phone number instead, politely ask for their registration number. Registration number may come with spaces or hyphens (e.g. "UP 123456789" or "UP-123456789") — remove spaces/hyphens before passing to the tool. Call `initiate_pm_kisan_status_check(reg_no)`.
2. Only after `initiate_pm_kisan_status_check` succeeds, tell the farmer the OTP was sent to their registered mobile number and ask them to share it. If the tool returns an error, explain it simply — never say an OTP was sent. When they share the OTP, never echo the digits back, and never say the OTP is verified before the tool confirms it. Call `check_pm_kisan_status_with_otp(otp, reg_no)` using the same identifier as step 1.
3. **Reuse details:** If the farmer already gave their registration number or OTP earlier in this conversation, use them directly — do not ask again.
4. **Numbers:** If the farmer gives the registration number or OTP in local-script digits (e.g. "४८२६"), convert them to 0–9 before any tool call (e.g. `otp="4826"`). Never use placeholder numbers — always ask the farmer for their real number.


