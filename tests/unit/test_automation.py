import pytest

from automation import AutomationEngine


def test_alias_expands_wildcard_and_multiple_commands():
    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_alias("k *", "kill %1;;say Attacking %1")

    assert engine.on_command("k goblin") == ["kill goblin", "say Attacking goblin"]


def test_gag_trigger_returns_none_and_sends_response():
    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(r"^You are hungry\.$", "eat bread", gag=True)

    assert engine.on_text("You are hungry.") is None
    assert sent == ["eat bread"]


def test_invalid_trigger_replacement_does_not_destroy_existing_entry():
    engine = AutomationEngine(send_fn=lambda _line: None)
    trigger_id = engine.add_simple_trigger(r"^hello$", "wave", trigger_id="fixed")
    with pytest.raises(Exception):
        engine.add_simple_trigger("[", "bad", trigger_id=trigger_id)

    assert engine.triggers[trigger_id].pattern == r"^hello$"
    assert engine.triggers[trigger_id].response_template == "wave"


def test_color_aware_trigger_matches_only_allowed_foregrounds():
    from ansi_parser import Segment, Style, StyledLine
    from automation import TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(
        r"^You're DYING!!$",
        "quaff healing-potion",
        style_filter=TriggerStyleFilter(
            foregrounds=((205, 0, 0), (255, 0, 0)),
        ),
    )

    dark_red = StyledLine([Segment("You're DYING!!", Style(fg=(205, 0, 0)))])
    bright_red = StyledLine([Segment("You're DYING!!", Style(fg=(255, 0, 0)))])
    chat_cyan = StyledLine([Segment("You're DYING!!", Style(fg=(0, 205, 205)))])

    assert engine.on_styled_text(dark_red) == "You're DYING!!"
    assert engine.on_styled_text(bright_red) == "You're DYING!!"
    assert engine.on_styled_text(chat_cyan) == "You're DYING!!"
    assert sent == ["quaff healing-potion", "quaff healing-potion"]


def test_style_trigger_does_not_fire_without_style_evidence():
    from automation import TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(
        r"DYING",
        "heal",
        style_filter=TriggerStyleFilter(foregrounds=((255, 0, 0),)),
    )

    engine.on_text("DYING")
    assert sent == []


def test_style_filter_rejects_mixed_color_match_span():
    from ansi_parser import Segment, Style, StyledLine
    from automation import TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(
        r"You're DYING!!",
        "heal",
        style_filter=TriggerStyleFilter(foregrounds=((255, 0, 0),)),
    )
    mixed = StyledLine([
        Segment("You're ", Style(fg=(0, 205, 205))),
        Segment("DYING!!", Style(fg=(255, 0, 0))),
    ])
    engine.on_styled_text(mixed)
    assert sent == []


def test_style_filter_accepts_mixed_prompt_when_all_captured_colors_are_allowed():
    from ansi_parser import Segment, Style, StyledLine
    from automation import TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(
        r"Please\ confirm\ account\ name,\ 'Northwest'\ \[Y/n\]\?",
        "n",
        style_filter=TriggerStyleFilter(
            foregrounds=((229, 229, 229), (205, 205, 0)),
        ),
    )

    prompt = StyledLine([
        Segment("Please confirm account name, '", Style(fg=(229, 229, 229))),
        Segment("Northwest", Style(fg=(205, 205, 0))),
        Segment("' [Y/n]?", Style(fg=(229, 229, 229))),
    ])

    assert engine.on_styled_text(prompt) == "Please confirm account name, 'Northwest' [Y/n]?"
    assert sent == ["n"]


def test_style_filter_can_match_default_foreground_with_explicit_accent_color():
    from ansi_parser import Segment, Style, StyledLine
    from automation import TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_simple_trigger(
        r"Please\ confirm\ account\ name,\ 'Northwest'\ \[Y/n\]\?",
        "n",
        style_filter=TriggerStyleFilter(
            foregrounds=((205, 205, 0),),
            allow_default_foreground=True,
            allow_default_background=True,
        ),
    )

    prompt = StyledLine([
        Segment("Please confirm account name, '", Style()),
        Segment("Northwest", Style(fg=(205, 205, 0))),
        Segment("' [Y/n]?", Style()),
    ])

    engine.on_styled_text(prompt)
    assert sent == ["n"]


def test_master_automation_switch_disables_aliases_triggers_and_timers():
    from automation import AutomationEngine

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_alias("l", "look")
    engine.add_simple_trigger(r"^danger$", "flee")
    engine.set_master_enabled(False)

    assert engine.on_command("l") == ["l"]
    assert engine.on_text("danger") == "danger"
    assert sent == []


def test_higher_priority_trigger_fires_first():
    from automation import AutomationEngine

    order = []
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_trigger(r"x", lambda _m, _c: order.append("low"), priority=-5)
    engine.add_trigger(r"x", lambda _m, _c: order.append("high"), priority=10)
    engine.add_trigger(r"x", lambda _m, _c: order.append("middle"), priority=0)

    engine.on_text("x")
    assert order == ["high", "middle", "low"]


def test_trigger_fire_hook_reports_exact_expanded_response():
    from automation import AutomationEngine

    sent = []
    fired = []
    engine = AutomationEngine(send_fn=sent.append)
    engine.add_trigger_fire_hook(fired.append)
    engine.add_simple_trigger(r"^Hello (.+)$", "say Hi %1;;wave")

    engine.on_text("Hello Ada")

    assert sent == ["say Hi Ada", "wave"]
    assert len(fired) == 1
    assert fired[0].matched_text == "Hello Ada"
    assert fired[0].response == "say Hi Ada;;wave"


def test_trigger_explain_reports_style_mismatch_without_firing():
    from ansi_parser import Segment, Style, StyledLine
    from automation import AutomationEngine, TriggerStyleFilter

    sent = []
    engine = AutomationEngine(send_fn=sent.append)
    trigger_id = engine.add_simple_trigger(
        r"^DANGER$", "heal",
        style_filter=TriggerStyleFilter(foregrounds=((255, 0, 0),)),
    )
    line = StyledLine([Segment("DANGER", Style(fg=(0, 205, 205)))])

    result = engine.explain_trigger(trigger_id, "DANGER", styled_line=line)

    assert result.regex_matched is True
    assert result.style_required is True
    assert result.style_matched is False
    assert result.would_fire is False
    assert sent == []


def test_legacy_attach_has_explicit_detach_lifetime():
    from client_core import EventBus, EventType

    seen = []
    engine = AutomationEngine(send_fn=lambda _line: None)
    engine.add_event_hook(seen.append)
    bus = EventBus()

    engine.attach(bus)
    bus.emit(EventType.TEXT, "one")
    engine.detach(bus)
    bus.emit(EventType.TEXT, "two")

    assert seen == ["one"]
