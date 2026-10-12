"""The storyboard a scenario's report folder makes in Artifacts > Flow."""

import json

from PySide6.QtGui import QColor, QImage

from cms_gui import flowview


def _png(path, height=600):
    # QImage, not QPixmap: these tests may run with no QApplication yet.
    image = QImage(400, height, QImage.Format_RGB32)
    image.fill(QColor("#336699"))
    assert image.save(str(path), "PNG")


def _report(folder, steps, status="pass", shots=()):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps({
        "scenario": "s", "session": "localhost:8069-admin", "status": status,
        "steps": steps}), encoding="utf-8")
    for name in shots:
        _png(folder / name)
    return folder


def _step(index, action="click", status="pass", target=".x", message="ok"):
    return {"index": index, "action": action, "status": status, "target": target,
            "value": None, "message": message, "duration_ms": 1234, "attempts": 1}


def test_each_step_gets_the_screenshot_named_after_it(tmp_path):
    folder = _report(tmp_path / "s", [_step(0), _step(1, "goto"), _step(2)],
                     shots=("screenshot_start.png", "screenshot_001.png",
                            "screenshot_003.png"))
    found = flowview.nodes(str(folder))
    assert [one["title"] for one in found] == ["Start", "1  click", "2  goto", "3  click"]
    assert [bool(one["shot"]) for one in found] == [True, True, False, True]
    assert found[1]["shot"].endswith("screenshot_001.png")
    assert found[1]["meta"] == "1.2 s"


def test_the_step_a_run_stopped_on_shows_the_finish_picture_and_why(tmp_path):
    folder = _report(tmp_path / "s", [_step(0), _step(1, status="error",
                                                     message="TimeoutError\nCall log:\nmore")],
                     status="error", shots=("screenshot_001.png", "screenshot_finish.png"))
    found = flowview.nodes(str(folder))
    failed = found[-1]
    assert failed["status"] == "error" and failed["shot"].endswith("screenshot_finish.png")
    assert failed["message"] == "TimeoutError\nCall log:\nmore"
    assert len(found) == 2, "the finish picture is the failed step's, not a card of its own"


def test_an_older_failure_report_uses_its_screenshot(tmp_path):
    folder = _report(tmp_path / "s", [_step(0, status="fail")], status="fail",
                     shots=("screenshot.png",))
    assert flowview.nodes(str(folder))[0]["shot"].endswith("screenshot.png")


def test_a_passed_run_s_finish_picture_is_a_card_at_the_end(tmp_path):
    folder = _report(tmp_path / "s", [_step(0)], shots=("screenshot_finish.png",))
    found = flowview.nodes(str(folder))
    assert [one["title"] for one in found] == ["1  click", "Finish"]


def test_a_folder_without_a_result_makes_no_flow(tmp_path):
    assert flowview.nodes(str(tmp_path)) is None


def test_the_view_lays_out_a_card_per_node_and_a_click_asks_for_the_picture(
        qapp, tmp_path, dispose):
    folder = _report(tmp_path / "s", [_step(0), _step(1)],
                     shots=("screenshot_001.png", "screenshot_002.png"))
    view = flowview.FlowView()
    try:
        view.resize(900, 700)
        assert view.show_folder(str(folder))
        view.show()
        qapp.processEvents()
        assert len(view.cards) == 2
        card = view.cards[0]
        assert card.property("status") == "pass"
        assert card.thumb.pixmap() is not None and not card.thumb.pixmap().isNull()
        # The top of a tall page, the width of the card - never a squeezed sliver.
        assert card.thumb.pixmap().width() == flowview.THUMB_WIDTH - 2
        picked = []
        view.picked.connect(picked.append)
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        QTest.mouseClick(card.thumb, Qt.LeftButton, pos=QPoint(10, 10))
        assert picked and picked[0].endswith("screenshot_001.png")
        assert not view.show_folder(str(tmp_path / "missing"))
    finally:
        dispose(view)
