from __future__ import annotations

from command_pipeline import CommandPipeline, CommandRequest, CommandSource


def make_pipeline(*, connected=True, server_echo=False):
    sent = []
    echoed = []
    history = []
    clients = []
    notices = []
    aliases = {"l": ["look"], "combo": ["north", "east"]}

    pipeline = CommandPipeline(
        is_connected=lambda: connected,
        server_echo_enabled=lambda: server_echo,
        send_line=sent.append,
        expand_aliases=lambda text: aliases.get(text, [text]),
        local_echo=echoed.append,
        record_history=history.append,
        handle_client_command=clients.append,
        notify_not_connected=lambda: notices.append("not connected"),
    )
    return pipeline, sent, echoed, history, clients, notices


def test_manual_command_uses_alias_history_and_local_echo():
    pipeline, sent, echoed, history, clients, notices = make_pipeline()

    result = pipeline.dispatch(CommandRequest("combo", CommandSource.MANUAL))

    assert result.sent == ("north", "east")
    assert sent == ["north", "east"]
    assert echoed == ["combo"]
    assert history == ["combo"]
    assert clients == []
    assert notices == []


def test_server_echo_suppresses_local_echo_for_manual_command():
    pipeline, sent, echoed, history, *_ = make_pipeline(server_echo=True)

    pipeline.dispatch(CommandRequest("secret", CommandSource.MANUAL))

    assert sent == ["secret"]
    assert echoed == []
    assert history == ["secret"]


def test_automation_bypasses_alias_history_and_client_commands():
    pipeline, sent, echoed, history, clients, notices = make_pipeline()

    result = pipeline.dispatch(CommandRequest("#disconnect", CommandSource.AUTOMATION))
    pipeline.dispatch(CommandRequest("l", CommandSource.AUTOMATION))

    assert result.handled_client_command is False
    assert sent == ["#disconnect", "l"]
    assert echoed == ["#disconnect", "l"]
    assert history == []
    assert clients == []
    assert notices == []


def test_macro_preserves_legacy_manual_policy():
    pipeline, sent, echoed, history, clients, _ = make_pipeline()

    pipeline.dispatch(CommandRequest("#status", CommandSource.MACRO))
    pipeline.dispatch(CommandRequest("l", CommandSource.MACRO))

    assert clients == ["status"]
    assert sent == ["look"]
    assert echoed == ["l"]
    assert history == ["l"]


def test_disconnected_manual_notifies_but_automation_fails_quietly():
    pipeline, sent, echoed, history, clients, notices = make_pipeline(connected=False)

    manual = pipeline.dispatch(CommandRequest("look", CommandSource.MANUAL))
    automated = pipeline.dispatch(CommandRequest("heal", CommandSource.AUTOMATION))

    assert manual.rejected_disconnected is True
    assert automated.rejected_disconnected is True
    assert notices == ["not connected"]
    assert sent == echoed == history == clients == []


def test_blank_manual_line_is_sent_but_not_echoed_or_stored():
    pipeline, sent, echoed, history, *_ = make_pipeline()

    pipeline.dispatch(CommandRequest("", CommandSource.MANUAL))

    assert sent == [""]
    assert echoed == []
    assert history == []


def test_variable_substitution_runs_after_alias_expansion():
    sent = []
    pipeline = CommandPipeline(
        is_connected=lambda: True,
        server_echo_enabled=lambda: False,
        send_line=sent.append,
        expand_aliases=lambda text: ["kill ${target}"] if text == "k" else [text],
        expand_variables=lambda text: text.replace("${target}", "goblin"),
        local_echo=lambda _text: None,
        record_history=lambda _text: None,
        handle_client_command=lambda _text: None,
        notify_not_connected=lambda: None,
    )

    result = pipeline.dispatch(CommandRequest("k", CommandSource.MANUAL))

    assert result.sent == ("kill goblin",)
    assert sent == ["kill goblin"]


def test_automation_commands_also_use_variable_substitution_without_aliases():
    sent = []
    pipeline = CommandPipeline(
        is_connected=lambda: True,
        server_echo_enabled=lambda: False,
        send_line=sent.append,
        expand_aliases=lambda text: ["wrong"],
        expand_variables=lambda text: text.replace("${heal}", "quaff potion"),
        local_echo=lambda _text: None,
        record_history=lambda _text: None,
        handle_client_command=lambda _text: None,
        notify_not_connected=lambda: None,
    )

    result = pipeline.dispatch(CommandRequest("${heal}", CommandSource.AUTOMATION))

    assert result.sent == ("quaff potion",)
    assert sent == ["quaff potion"]
