from lenkobot.telegram_presentation import TelegramCommand, parse_telegram_command


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
