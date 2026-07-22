from lenkobot.telegram_presentation import (
    TELEGRAM_COMMANDS,
    TelegramCommand,
    TelegramInlineButton,
    TelegramWebSource,
    TelegramSentMessage,
    confirmation_callback_data,
    forget_callback_data,
    memories_page_callback_data,
    parse_confirmation_callback_data,
    parse_forget_callback_data,
    parse_memories_page_callback_data,
    parse_persona_callback_data,
    parse_telegram_command,
    persona_callback_data,
    render_sources_html,
    split_telegram_text,
)


def test_parser_handles_persona_command_and_bot_suffix():
    assert parse_telegram_command(" /PERSONA@lenkobot analyst ") == TelegramCommand(
        name="persona",
        arguments=("analyst",),
    )


def test_parser_returns_none_for_regular_text():
    assert parse_telegram_command("/not a command") == TelegramCommand(
        name="not",
        arguments=("a", "command"),
    )
    assert parse_telegram_command("hello") is None


def test_command_catalog_contains_user_facing_commands_without_internal_reload():
    assert [item.command for item in TELEGRAM_COMMANDS] == [
        "start",
        "help",
        "persona",
        "new",
        "remind",
        "tasks",
        "timezone",
        "quiet",
        "remember",
        "memories",
        "forget",
    ]
    assert all(item.command == item.command.casefold() for item in TELEGRAM_COMMANDS)
    assert all(item.description for item in TELEGRAM_COMMANDS)


def test_persona_callback_payload_is_versioned_and_bounded():
    payload = persona_callback_data("analyst")

    assert payload == "persona:v1:analyst"
    assert parse_persona_callback_data(payload) == "analyst"
    assert parse_persona_callback_data("persona:v2:analyst") is None
    assert parse_persona_callback_data("other:v1:analyst") is None
    assert parse_persona_callback_data("persona:v1:") is None
    assert parse_persona_callback_data("persona:v1:" + ("x" * 64)) is None


def test_inline_button_is_a_typed_transport_neutral_value():
    assert TelegramInlineButton(text="Analyst", callback_data="persona:v1:analyst") == (
        TelegramInlineButton(text="Analyst", callback_data="persona:v1:analyst")
    )


def test_sent_message_handle_is_transport_neutral():
    handle = TelegramSentMessage(chat_id=500, message_id=42)

    assert handle.chat_id == 500
    assert handle.message_id == 42


def test_sources_html_is_bounded_escaped_and_deduplicated():
    rendered = render_sources_html(
        (
            TelegramWebSource(
                title='<Fresh & "safe">',
                url="https://example.com/current?a=1&b=2",
            ),
            TelegramWebSource(
                title="Duplicate",
                url="https://example.com/current?a=1&b=2",
            ),
            TelegramWebSource(title="Unsafe", url="javascript:alert(1)"),
        )
    )

    assert rendered.startswith("<b>Источники:</b>\n")
    assert '&lt;Fresh &amp; &quot;safe&quot;&gt;' in rendered
    assert 'href="https://example.com/current?a=1&amp;b=2"' in rendered
    assert "Duplicate" not in rendered
    assert "Unsafe" not in rendered
    assert len(rendered) <= 4096


def test_confirmation_callback_payload_roundtrip_and_rejects_foreign_data():
    confirm = confirmation_callback_data("confirm", "token123")
    cancel = confirmation_callback_data("cancel", "token123")

    assert confirm == "confirm:v1:token123"
    assert cancel == "cancel:v1:token123"
    assert parse_confirmation_callback_data(confirm) == ("confirm", "token123")
    assert parse_confirmation_callback_data(cancel) == ("cancel", "token123")
    assert parse_confirmation_callback_data("confirm:v2:token123") is None
    assert parse_confirmation_callback_data("persona:v1:token123") is None
    assert parse_confirmation_callback_data("confirm:v1:") is None
    assert parse_confirmation_callback_data("other") is None


def test_confirmation_callback_payload_is_bounded():
    try:
        confirmation_callback_data("confirm", "x" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized confirmation token must be rejected")


def test_memories_page_callback_payload_roundtrip():
    payload = memories_page_callback_data(3)

    assert payload == "mem:v1:3"
    assert parse_memories_page_callback_data(payload) == 3
    assert parse_memories_page_callback_data("mem:v1:0") is None
    assert parse_memories_page_callback_data("mem:v1:-1") is None
    assert parse_memories_page_callback_data("mem:v1:x") is None
    assert parse_memories_page_callback_data("mem:v2:3") is None
    assert parse_memories_page_callback_data("persona:v1:3") is None


def test_forget_callback_payload_roundtrip():
    payload = forget_callback_data(17)

    assert payload == "forget:v1:17"
    assert parse_forget_callback_data(payload) == 17
    assert parse_forget_callback_data("forget:v1:0") is None
    assert parse_forget_callback_data("forget:v1:-2") is None
    assert parse_forget_callback_data("forget:v1:abc") is None
    assert parse_forget_callback_data("forget:v2:17") is None


def test_split_telegram_text_keeps_short_text_untouched():
    assert split_telegram_text("hello") == ("hello",)
    assert split_telegram_text("x" * 4096) == ("x" * 4096,)


def test_split_telegram_text_prefers_paragraph_then_line_then_space_boundaries():
    paragraph_text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    chunks = split_telegram_text(paragraph_text)
    assert chunks == ("a" * 3000, "b" * 3000)

    line_text = ("a" * 3000) + "\n" + ("b" * 3000)
    chunks = split_telegram_text(line_text)
    assert chunks == ("a" * 3000, "b" * 3000)

    space_text = ("a" * 3000) + " " + ("b" * 3000)
    chunks = split_telegram_text(space_text)
    assert chunks == ("a" * 3000, "b" * 3000)


def test_split_telegram_text_hard_cuts_without_boundaries():
    chunks = split_telegram_text("x" * 5000)

    assert chunks == ("x" * 4096, "x" * 904)
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_telegram_text_handles_multiple_chunks_and_avoids_empty_chunks():
    text = "\n\n".join(["x" * 4000] * 4)
    chunks = split_telegram_text(text)

    assert len(chunks) >= 3
    assert all(0 < len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks).replace("\n\n", "") == "x" * 16000
