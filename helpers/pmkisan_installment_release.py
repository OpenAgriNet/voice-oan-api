from datetime import date


def get_pm_kisan_23rd_installment_release_section() -> str:
    """
    Returns a date-aware PM-KISAN 23rd instalment release section.
    Uses future tense on or before 20 June 2026; past tense from 21 June 2026 onward.
    """
    release_date = date(2026, 6, 20)
    today = date.today()

    if today <= release_date:
        answer_en = (
            "The 23rd instalment of PM-KISAN is scheduled to be released on 20 June 2026. "
            "Eligible farmers will receive ₹2,000 directly in their bank accounts."
        )
        answer_hi = (
            "PM-KISAN की 23वीं किस्त 20 जून 2026 को जारी की जाएगी। "
            "पात्र किसानों के बैंक खातों में सीधे ₹2,000 भेजे जाएंगे।"
        )
    else:
        answer_en = (
            "The 23rd instalment of PM-KISAN was released on 20 June 2026. "
            "Eligible farmers received ₹2,000 directly in their bank accounts."
        )
        answer_hi = (
            "PM-KISAN की 23वीं किस्त 20 जून 2026 को जारी की जा चुकी है। "
            "पात्र किसानों के बैंक खातों में सीधे ₹2,000 भेजे गए।"
        )

    return (
        "## PM-KISAN 23rd Instalment Release\n\n"
        f"**Answer (English):** {answer_en}\n\n"
        f"**Answer (Hindi):** {answer_hi}\n\n"
        "**Source:** Government Scheme Information"
    )
