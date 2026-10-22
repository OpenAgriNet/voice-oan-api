---
name: pmfby
description: PMFBY crop insurance status, OTP verification and grievance workflows.
tools:
- check_pmfby_grievance_otp
- check_pmfby_status_with_otp
- initiate_pmfby_grievance_otp
- initiate_pmfby_status_check
- pmfby_grievance_status
- pmfby_submit_grievance
sections:
  grievance:
    file: grievance/pmfby_grievance_{language}.md
  status:
    file: status/pmfby_status_{language}.md
  grievance_routing:
    file: grievance_routing/pmfby_grievance_routing_{language}.md
---

# Pmfby

Edit the localized files named above to change this workflow. Tool names refer to
the shared Python registry; listing a tool in another skill never duplicates its
schema or implementation. Loading policy is configured separately.
