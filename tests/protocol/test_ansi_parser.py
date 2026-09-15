from ansi_parser import AnsiParser


def test_ansi_color_is_preserved_in_styled_segments():
    parser = AnsiParser()
    lines = parser.feed("\x1b[31mred\x1b[0m plain\r\n")

    assert len(lines) == 1
    assert lines[0].plain_text() == "red plain"
    assert lines[0].segments[0].style.fg is not None


def test_split_escape_sequence_survives_feed_boundary():
    parser = AnsiParser()
    assert parser.feed("\x1b[3") == []
    lines = parser.feed("2mgreen\n")

    assert len(lines) == 1
    assert lines[0].plain_text() == "green"


def test_prompt_can_be_flushed_without_newline():
    parser = AnsiParser()
    assert parser.feed("HP:100>") == []
    prompt = parser.flush_line()

    assert prompt is not None
    assert prompt.plain_text() == "HP:100>"


def test_logical_line_has_defensive_character_limit():
    from ansi_parser import AnsiParseLimitError, AnsiParser

    parser = AnsiParser(max_line_chars=8)
    parser.feed("12345678")

    try:
        parser.feed("9")
    except AnsiParseLimitError as exc:
        assert "logical ANSI line exceeds" in str(exc)
    else:
        raise AssertionError("unterminated ANSI line exceeded its limit")


def test_parser_reset_recovers_after_line_limit_error():
    from ansi_parser import AnsiParseLimitError, AnsiParser

    parser = AnsiParser(max_line_chars=4)
    parser.feed("1234")
    try:
        parser.feed("5")
    except AnsiParseLimitError:
        pass
    parser.reset()

    lines = parser.feed("ok\n")
    assert [line.plain_text() for line in lines] == ["ok"]


def test_oversized_osc_is_discarded_across_feeds_without_becoming_visible_text():
    import ansi_parser

    parser = AnsiParser()
    payload = "X" * (ansi_parser._MAX_OSC_LEN + 32)

    # No terminator yet: the control payload is discarded and the parser enters
    # bounded discard mode instead of rendering the OSC body as MUD text.
    assert parser.feed("\x1b]0;" + payload) == []
    assert parser.current_line().plain_text() == ""

    # More OSC data is still hidden.  Once BEL arrives, ordinary text after the
    # terminator is parsed normally.
    assert parser.feed("still-hidden") == []
    lines = parser.feed("\x07visible\n")
    assert [line.plain_text() for line in lines] == ["visible"]


def test_oversized_dcs_st_terminator_split_across_feeds_recovers_cleanly():
    import ansi_parser

    parser = AnsiParser()
    payload = "Y" * (ansi_parser._MAX_OSC_LEN + 8)
    assert parser.feed("\x1bP" + payload + "\x1b") == []
    assert parser.current_line().plain_text() == ""

    # The backslash completes ST even though ESC was the last char of the
    # previous feed.
    lines = parser.feed("\\after\n")
    assert [line.plain_text() for line in lines] == ["after"]


def test_oversized_csi_is_discarded_until_final_byte_then_recovers():
    import ansi_parser

    parser = AnsiParser()
    oversized_params = "1;" * ansi_parser._MAX_CSI_LEN
    assert parser.feed("\x1b[" + oversized_params) == []
    assert parser.current_line().plain_text() == ""

    # 'm' terminates the discarded CSI; only following visible text survives.
    lines = parser.feed("mrecovered\n")
    assert [line.plain_text() for line in lines] == ["recovered"]


def test_reset_exits_control_discard_mode():
    import ansi_parser

    parser = AnsiParser()
    parser.feed("\x1b]0;" + "Z" * (ansi_parser._MAX_OSC_LEN + 1))
    parser.reset()

    lines = parser.feed("normal\n")
    assert [line.plain_text() for line in lines] == ["normal"]
