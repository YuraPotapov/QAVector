"""Pictures a tool names become links in its row; nothing is loaded until one is clicked."""

from cms_gui import imagelinks


def test_only_pictures_that_are_there_become_links(tmp_path):
    shot = tmp_path / "before.png"
    shot.write_bytes(b"\x89PNG")
    text = "file_path: %s\nalso /nowhere/gone.png and %s/notes.txt" % (shot, tmp_path)
    assert imagelinks.found(text) == [str(shot)]
    rich = imagelinks.linked(text)
    assert rich.count("<a href=") == 1 and "file://" in rich and "/nowhere/gone.png" in rich
    assert "white-space: pre-wrap" in rich
    assert imagelinks.linked("nothing to see /tmp/a.txt") is None


def test_a_chat_tool_row_links_the_picture_it_read(qapp, tmp_path, monkeypatch):
    from cms_gui.pages.agentchat import ChatFeed
    shot = tmp_path / "shot.PNG"
    shot.write_bytes(b"\x89PNG")
    opened = []
    monkeypatch.setattr(imagelinks.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    feed = ChatFeed()
    feed.load([{"type": "tool.started", "id": "t1", "name": "Read", "title": "Read shot.PNG",
                "detail": "file_path: %s" % shot}])
    _kind, row = feed._rows[-1]
    assert "<a href=" in row.detail.text()
    row.detail.linkActivated.emit(imagelinks.linked(str(shot)).split('"')[3])
    assert opened == [str(shot)]
    feed.deleteLater()
