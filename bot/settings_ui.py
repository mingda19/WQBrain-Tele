"""Settings keyboards shared by the /sim and /batch conversations.

Both flows pick the same seven settings, so the keyboards live here rather than
being duplicated. Every builder takes a callback ``prefix`` because each
conversation owns a disjoint namespace -- ``sim:`` and ``bat:`` -- and a shared
prefix would let one conversation's ConversationHandler intercept the other's
buttons.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.alpha_spec import TEST_PERIOD_CHOICES, AlphaSpec, SettingsCatalog

CHOICE_LABELS = {
    "region": "Region",
    "universe": "Universe",
    "delay": "Delay",
    "neutralization": "Neutral",
    "test_period": "Period",
}


def choices_for(catalog: SettingsCatalog, spec: AlphaSpec, field: str) -> list:
    """Valid values for one setting, given what the rest of the spec is set to."""
    if field == "region":
        return catalog.regions()
    if field == "delay":
        return catalog.delays(spec.region)
    if field == "universe":
        return catalog.universes(spec.region, spec.delay)
    if field == "neutralization":
        return catalog.neutralizations(spec.region, spec.delay)
    if field == "test_period":
        return list(TEST_PERIOD_CHOICES)
    return []


def settings_keyboard(
    spec: AlphaSpec, prefix: str, *, run_label: str = "Run simulation"
) -> InlineKeyboardMarkup:
    """Each button shows the value it currently holds."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Region: {spec.region}", callback_data=f"{prefix}:pick:region"
                ),
                InlineKeyboardButton(
                    f"Universe: {spec.universe}", callback_data=f"{prefix}:pick:universe"
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Delay: {spec.delay}", callback_data=f"{prefix}:pick:delay"
                ),
                InlineKeyboardButton(
                    f"Neutral: {spec.neutralization}",
                    callback_data=f"{prefix}:pick:neutralization",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Decay: {spec.decay}", callback_data=f"{prefix}:edit:decay"
                ),
                InlineKeyboardButton(
                    f"Trunc: {spec.truncation:g}",
                    callback_data=f"{prefix}:edit:truncation",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Test period: {spec.test_period}",
                    callback_data=f"{prefix}:pick:test_period",
                )
            ],
            [
                InlineKeyboardButton(run_label, callback_data=f"{prefix}:run"),
                InlineKeyboardButton("Cancel", callback_data=f"{prefix}:cancel"),
            ],
        ]
    )


def choice_keyboard(field: str, options: list, current, prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{'• ' if str(option) == str(current) else ''}{option}",
            callback_data=f"{prefix}:set:{field}:{option}",
        )
        for option in options
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("Back", callback_data=f"{prefix}:menu")])
    return InlineKeyboardMarkup(rows)


def edit_hint(field: str) -> str:
    if field == "decay":
        return "Send a decay value (whole number, 0–512):"
    return "Send a truncation value (0–1, e.g. 0.08):"
