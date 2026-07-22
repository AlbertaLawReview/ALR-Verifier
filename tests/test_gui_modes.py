import ctypes
import queue
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import gui


def test_log_redirect_never_calls_tk_and_coalesces_one_write():
    captured = []
    doc = object()
    redirect = gui._LogRedirect(
        captured.append, resolve=lambda _ident: doc,
    )

    redirect.write("first\nsecond\n")
    redirect.flush()

    assert captured == [(doc, "first\nsecond\n")]

    quiet = gui._LogRedirect(captured.append, quiet=True)
    quiet.write("hidden")
    assert captured == [(doc, "first\nsecond\n")]


def test_log_poll_is_bounded_and_coalesces_queue_entries():
    pending = queue.Queue()
    doc = object()
    for index in range(250):
        pending.put((doc, f"line {index}\n"))
    poll_again = Mock()
    stub = SimpleNamespace(
        log_queue=pending,
        _log=Mock(),
        _release_parallel_slots=Mock(),
        running=False,
        root=SimpleNamespace(after=Mock()),
        _poll_log=poll_again,
    )

    gui.ALRQuoteVerifierGUI._poll_log(stub)

    assert pending.qsize() == 150
    stub._log.assert_called_once_with(
        "".join(f"line {index}\n" for index in range(100)), doc=doc,
    )
    stub.root.after.assert_called_once_with(10, poll_again)


def test_log_batch_uses_one_text_widget_insert_call():
    text = Mock()
    text.index.return_value = "2.0"
    stub = SimpleNamespace(
        doc_views=[],
        log_text=text,
        detail_log_var=SimpleNamespace(get=lambda: False),
    )

    gui.ALRQuoteVerifierGUI._log(
        stub, "12:00  first line\n12:01  second line\n",
    )

    assert text.insert.call_count == 1
    text.see.assert_not_called()


def test_database_chatter_does_not_repaint_progress_view():
    stub = SimpleNamespace(now=Mock())

    gui.DocProgressView.feed(stub, "[DB] local lookup detail", "")

    stub.now.assert_not_called()


def test_gui_mode_and_supra_settings_reach_engine_args():
    assert gui.RUN_MODE_LABELS == {
        "High accuracy": "high_accuracy",
        "Economy": "economy",
        "Ultra economy": "ultra_economy",
        "Free (no AI calls)": "free",
    }
    for run_mode in gui.VALID_RUN_MODES:
        args = gui._build_args(
            False, run_mode=run_mode, supra_linking="aggressive"
        )
        assert args.run_mode == run_mode
        assert args.supra_linking == "aggressive"
    assert gui._build_args(False, local_only=True).local_only is True

    settings = gui._settings_with_defaults({
        "run_mode": "free", "supra_linking": "aggressive",
    })
    assert settings["run_mode"] == "free"
    assert settings["supra_linking"] == "aggressive"


def test_gui_keeps_source_apis_enabled():
    dry_fire, use_db_search = gui._source_run_flags(None)

    assert not dry_fire
    assert use_db_search
    args = gui._build_args(dry_fire, use_a2aj=True, use_db_search=use_db_search)
    assert args.use_a2aj
    assert args.use_db_search
    assert gui.DEFAULT_GUI_SETTINGS["us_uk_case_lookup"] is True
    assert gui.DEFAULT_GUI_SETTINGS["export_detail"] == "diagnostic-hidden"


def test_run_warns_before_partial_footnote_run():
    from types import SimpleNamespace

    stub = SimpleNamespace(
        running=False,
        files=["doc.docx"],
        run_mode_var=SimpleNamespace(get=lambda: "Free (no AI calls)"),
        fn_filter_var=SimpleNamespace(get=lambda: "1-5"),
        out_var=SimpleNamespace(set=lambda value: None),
        _ensure_api_key=lambda: True,
    )
    with patch.object(gui.messagebox, "askyesno", return_value=False) as ask, \
            patch.object(gui.aqv, "_configure_from_args") as configure:
        gui.ALRQuoteVerifierGUI._run(stub)

    ask.assert_called_once()
    configure.assert_not_called()


def test_run_does_not_warn_without_footnote_filter():
    stub = SimpleNamespace(
        running=False,
        files=[],
        run_mode_var=SimpleNamespace(get=lambda: "Free (no AI calls)"),
        fn_filter_var=SimpleNamespace(get=lambda: ""),
        out_var=SimpleNamespace(set=lambda value: None),
        _ensure_api_key=lambda: True,
    )
    with patch.object(gui.messagebox, "askyesno") as ask, \
            patch.object(gui.messagebox, "showwarning"):
        gui.ALRQuoteVerifierGUI._run(stub)

    ask.assert_not_called()


def test_setup_drop_keeps_handler_alive():
    handler = object()
    stub = SimpleNamespace(root=object(), _on_drop=Mock(), _log=Mock())
    with patch.object(gui, "DRAG_DROP_AVAILABLE", True), patch.object(
        gui, "DropHandler", create=True, return_value=handler
    ) as drop_handler:
        gui.ALRQuoteVerifierGUI._setup_drop(stub)

    drop_handler.assert_called_once_with(stub.root, stub._on_drop)
    assert stub._drop_handler is handler


def test_windows_drop_wndproc_uses_pointer_width():
    if gui.DRAG_DROP_AVAILABLE:
        assert ctypes.sizeof(gui.LONG_PTR) == ctypes.sizeof(ctypes.c_void_p)
        assert gui.SetWindowLongPtrW is not None


def test_raw_log_follows_selected_article_and_keeps_global_lines():
    text = Mock()
    text.index.return_value = "1.0"
    docs = [
        SimpleNamespace(index=0, feed=Mock()),
        SimpleNamespace(index=1, feed=Mock()),
    ]
    stub = SimpleNamespace(
        doc_nb=SimpleNamespace(index=lambda _which: 1),
        doc_views=docs,
        log_text=text,
    )

    gui.ALRQuoteVerifierGUI._log(stub, "global\n")
    gui.ALRQuoteVerifierGUI._log(stub, "article\n", doc=docs[1])
    gui.ALRQuoteVerifierGUI._filter_raw_log(stub)

    assert text.insert.call_args_list == [
        call(gui.tk.END, "global\n", ("cont",)),
        call(gui.tk.END, "[2] article\n", ("cont", "doc_1")),
    ]
    assert text.tag_configure.call_args_list == [
        call("doc_0", elide=True),
        call("doc_1", elide=False),
    ]


def test_first_local_only_toggle_offers_install_and_leaves_toggle_off():
    enabled = SimpleNamespace(get=lambda: True, set=Mock())
    stub = SimpleNamespace(
        local_only_var=enabled,
        _a2aj_corpus_installed=lambda: False,
        _offer_local_corpus_install=lambda: True,
        _start_a2aj_install=Mock(),
        _apply_local_only_ui=Mock(),
    )

    gui.ALRQuoteVerifierGUI._on_local_only_toggle(stub)

    enabled.set.assert_called_once_with(False)
    stub._start_a2aj_install.assert_called_once_with(
        enable_local_only=True, confirmed=True,
    )


def test_a2aj_install_clears_cache_after_each_completed_corpus():
    snapshots = (
        SimpleNamespace(kind="cases", size=10),
        SimpleNamespace(kind="laws", size=20),
    )
    corpus = Mock()
    corpus.fetch_metadata.side_effect = snapshots
    corpus.install_or_update.side_effect = [None, RuntimeError("laws failed")]
    cancel = Mock()
    cancel.is_set.return_value = False
    stub = SimpleNamespace(
        _a2aj_installing=False,
        _a2aj_corpus_installed=lambda: True,
        _a2aj_cancel=cancel,
        a2aj_corpus_btn=Mock(),
        a2aj_corpus_status_var=Mock(),
        root=SimpleNamespace(after=Mock()),
        _finish_a2aj_install=Mock(),
        _show_a2aj_progress=Mock(),
    )

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    with patch.object(
        gui.aqv.a2aj_client, "get_local_corpus", return_value=corpus
    ), patch.object(
        gui.aqv.a2aj_client, "clear_memory_cache"
    ) as clear, patch.object(gui.threading, "Thread", ImmediateThread):
        gui.ALRQuoteVerifierGUI._start_a2aj_install(stub, confirmed=True)

    assert corpus.install_or_update.call_count == 2
    clear.assert_called_once_with()


def test_cli_smoke_hook_runs_engine_and_counts_live_transport_calls(capsys):
    argv = ["--input", "fixture", "--footnote-ids", "1"]
    transport = Mock(return_value=object())

    def run_engine(actual):
        assert actual == argv
        gui.aqv._llm_call(model="gpt-5.2")

    with patch.dict(gui.os.environ, {
        "ALR_CLI_SMOKE_ARGS": gui.json.dumps(argv),
    }), patch.object(gui.aqv, "_llm_call", transport), patch.object(
        gui.aqv, "_main", side_effect=run_engine,
    ):
        assert gui._run_cli_smoke_from_env()

    transport.assert_called_once_with(model="gpt-5.2")
    assert "CLI smoke: live LLM calls=1" in capsys.readouterr().out


def test_process_log_hook_redirects_windowed_output(tmp_path):
    destination = tmp_path / "packaged.log"
    old_stdout, old_stderr = gui.sys.stdout, gui.sys.stderr
    stream = None
    try:
        with patch.dict(gui.os.environ, {"ALR_PROCESS_LOG": str(destination)}):
            stream = gui._configure_process_log_from_env()
            print("packaged progress", flush=True)
    finally:
        gui.sys.stdout, gui.sys.stderr = old_stdout, old_stderr
        if stream is not None:
            stream.close()

    assert destination.read_text(encoding="utf-8") == "packaged progress\n"
