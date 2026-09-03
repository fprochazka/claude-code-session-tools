from cc_session_tools.titles import clean_title


def test_clean_title_plain_prompt():
    assert clean_title("fix the parser bug") == "fix the parser bug"


def test_clean_title_strips_system_reminder():
    assert clean_title("<system-reminder>ctx</system-reminder>real ask") == "real ask"


def test_clean_title_slash_command():
    raw = "<command-name>/pre-plan</command-name><command-args>do x</command-args>"
    assert clean_title(raw) == "/pre-plan — do x"


def test_clean_title_shell_command():
    assert clean_title("<bash-input>git status</bash-input>") == "$ git status"


def test_clean_title_empty():
    assert clean_title(None) == "(no prompt)"
    assert clean_title("<system-reminder>only noise</system-reminder>") == "(no prompt)"
