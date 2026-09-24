"""Where the app reads and writes, in a checkout and in an installed build.

The whole point of runtime_paths is that these two answers differ, so the tests
are written the same way: each case pins one of the two shapes.
"""

import os
import sys

import pytest

import runtime_paths as rp


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Pretend to be an installed build with the home directory under tmp_path."""
    bundle = tmp_path / "opt" / "_internal"
    bundle.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(rp, "FROZEN", True)
    monkeypatch.setattr("sys._MEIPASS", str(bundle), raising=False)
    # Every variable expanduser("~") consults, on either platform: $HOME is not
    # one of them on Windows, where setting it alone leaves these tests writing
    # a users.json into the real %USERPROFILE%\QAVector.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", os.path.splitdrive(str(home))[0])
    monkeypatch.setenv("HOMEPATH", os.path.splitdrive(str(home))[1])
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    assert os.path.expanduser("~") == str(home)   # the redirection actually took
    return bundle, home


# --------------------------------------------------- the installer's choice

def _install(tmp_path, monkeypatch, text):
    """A frozen build at <install>/core/, with cms.ini beside it."""
    install = tmp_path / "install"
    core = install / "core"
    core.mkdir(parents=True)
    (install / "cms.ini").write_text(text, encoding="utf-8")
    monkeypatch.setattr(rp, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(core / "qavector-core.exe"))
    return install


def test_the_folder_chosen_at_install_time_wins_over_the_default(tmp_path, monkeypatch):
    # The whole point of the wizard page: reports and sessions go where the
    # person said, not into a home directory they never picked.
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    chosen = tmp_path / "D" / "Projects" / "CMS"
    _install(tmp_path, monkeypatch, "[Paths]\ndata_dir=%s\n" % chosen)
    assert rp.user_data_root() == str(chosen)
    assert rp.reports_dir() == os.path.join(str(chosen), "reports")


def test_cms_home_still_beats_the_installed_choice(tmp_path, monkeypatch):
    # A test run, or a second copy against a scratch directory, must never be
    # able to reach the real one.
    _install(tmp_path, monkeypatch,
             "[Paths]\ndata_dir=%s\n" % (tmp_path / "chosen"))
    monkeypatch.setenv(rp.HOME_ENV, str(tmp_path / "scratch"))
    assert rp.user_data_root() == str(tmp_path / "scratch")


def test_an_ini_without_the_key_is_simply_not_an_answer(tmp_path, monkeypatch):
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    _install(tmp_path, monkeypatch, "[Paths]\n; nothing useful here\n")
    assert rp.user_data_root() == os.path.join(str(tmp_path / "home"),
                                               rp.USER_DIR_NAME)


def test_a_missing_ini_leaves_the_default_alone(tmp_path, monkeypatch):
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    monkeypatch.setattr(rp, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "install" / "core" / "core.exe"))
    assert rp.configured_data_root() == ""


def test_a_checkout_ignores_any_cms_ini(tmp_path, monkeypatch):
    # Not frozen: the checkout is its own data root and nothing beside a
    # developer's python.exe gets to redirect it.
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    _install(tmp_path, monkeypatch,
             "[Paths]\ndata_dir=%s\n" % (tmp_path / "chosen"))
    monkeypatch.setattr(rp, "FROZEN", False)
    assert rp.configured_data_root() == ""
    assert rp.user_data_root() == rp.app_root()


# ------------------------------------------------------------------ checkout

def test_a_checkout_keeps_everything_in_one_place():
    # The property the whole design rests on: nothing about a source checkout
    # changes, so no existing profile, report or users.json moves.
    root = os.path.dirname(os.path.abspath(rp.__file__))
    assert rp.app_root() == root
    assert rp.user_data_root() == root
    assert rp.sessions_dir() == os.path.join(root, "user_sessions")
    assert rp.config_path() == os.path.join(root, "users.json")


def test_a_checkout_is_not_given_a_users_json(tmp_path, monkeypatch):
    # Its absence is meaningful there - it is what sends you to --init-users-json.
    monkeypatch.setenv(rp.HOME_ENV, str(tmp_path))
    monkeypatch.setattr(rp, "app_root", lambda: str(tmp_path))
    rp.ensure_user_data_root()
    assert not (tmp_path / "users.json").exists()


# -------------------------------------------------------------------- frozen

def test_frozen_splits_resources_from_user_data(frozen):
    bundle, home = frozen
    assert rp.app_root() == str(bundle)
    assert rp.bundled_flows_dir() == os.path.join(str(bundle), "flows")
    assert rp.user_data_root() == os.path.join(str(home), rp.USER_DIR_NAME)
    assert rp.reports_dir().startswith(str(home))


def test_frozen_searches_the_users_flows_before_the_bundled_ones(frozen):
    # The order is the feature: a recorded scenario shadows a bundled one of the
    # same id, and a scenario in the user's tree can still use: the shipped blocks.
    bundle, home = frozen
    assert rp.flows_search_path() == [
        os.path.join(str(home), rp.USER_DIR_NAME, "flows"),
        os.path.join(str(bundle), "flows"),
    ]
    # What gets written is the user's tree, never the bundle.
    assert rp.flows_dir() == rp.user_flows_dir()


def test_a_checkout_has_one_flows_tree():
    # Both roles are the same directory there, so the search path collapses and a
    # checkout resolves flows exactly as it did before there was a search path.
    assert rp.flows_search_path() == [rp.bundled_flows_dir()]


def test_cycles_are_laid_out_the_same_way_flows_are(frozen):
    # The same two-tree arrangement for the same reason, so a cycle the user
    # edited shadows a bundled one and an upgrade cannot overwrite theirs.
    bundle, home = frozen
    assert rp.cycles_search_path() == [
        os.path.join(str(home), rp.USER_DIR_NAME, "cycles"),
        os.path.join(str(bundle), "cycles"),
    ]
    assert rp.cycles_dir() == rp.user_cycles_dir()


def test_a_checkout_has_one_cycles_tree():
    assert rp.cycles_search_path() == [rp.bundled_cycles_dir()]


def test_cycle_runs_sit_beside_reports_not_inside_them(frozen):
    # A report directory is what one scenario left behind; a cycle run is the
    # whole of an execution. Mixing them would make both harder to clean up.
    bundle, home = frozen
    assert rp.cycle_runs_dir() == os.path.join(str(home), rp.USER_DIR_NAME,
                                               "cycle-runs")
    assert rp.cycle_runs_dir() != rp.reports_dir()


def test_first_run_creates_somewhere_to_put_scenarios(frozen):
    bundle, home = frozen
    root = rp.ensure_user_data_root()
    assert os.path.isdir(os.path.join(root, "flows", "scenarios"))


def test_first_run_creates_somewhere_to_put_cycles_and_their_runs(frozen):
    bundle, home = frozen
    root = rp.ensure_user_data_root()
    assert os.path.isdir(os.path.join(root, "cycles"))
    assert os.path.isdir(os.path.join(root, "cycle-runs"))


def test_cms_home_overrides_everything(frozen, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    os.environ[rp.HOME_ENV] = str(elsewhere)
    try:
        assert rp.user_data_root() == str(elsewhere)
    finally:
        del os.environ[rp.HOME_ENV]


def test_first_run_creates_the_directories_and_seeds_the_config(frozen):
    bundle, home = frozen
    (bundle / "users.example.json").write_text('{"users": []}', encoding="utf-8")
    root = rp.ensure_user_data_root()

    assert os.path.isdir(os.path.join(root, "user_sessions"))
    assert os.path.isdir(os.path.join(root, "reports"))
    config = os.path.join(root, "users.json")
    assert open(config, encoding="utf-8").read() == '{"users": []}'
    # It grows real passwords, so it is never world-readable, not even briefly.
    # Only where mode bits are the mechanism: Windows has none that os.chmod can
    # set (it toggles the read-only flag and reports 0o666), and access there is
    # governed by the ACL the file inherits from the user's own profile.
    if os.name != "nt":
        assert oct(os.stat(config).st_mode & 0o777) == "0o600"


def test_first_run_never_overwrites_existing_user_data(frozen):
    bundle, home = frozen
    (bundle / "users.example.json").write_text("{}", encoding="utf-8")
    root = rp.ensure_user_data_root()
    config = os.path.join(root, "users.json")
    with open(config, "w", encoding="utf-8") as fh:
        fh.write('{"mine": true}')
    profile = os.path.join(root, "user_sessions", "localhost-admin")
    os.makedirs(profile)

    rp.ensure_user_data_root()   # an upgrade, or simply the next launch

    assert open(config, encoding="utf-8").read() == '{"mine": true}'
    assert os.path.isdir(profile)


# ----------------------------------------------- from chrome-multi-session

@pytest.mark.skipif(os.name == "nt", reason="the link is a junction there")
def test_the_old_folder_moves_on_first_start_and_leaves_a_link(frozen):
    bundle, home = frozen
    old = home / rp.LEGACY_USER_DIR_NAME
    (old / "user_sessions" / "localhost-admin").mkdir(parents=True)
    (old / "users.json").write_text('{"mine": true}', encoding="utf-8")
    root = rp.ensure_user_data_root()
    assert root == str(home / rp.USER_DIR_NAME)
    assert open(os.path.join(root, "users.json"), encoding="utf-8").read() == '{"mine": true}'
    assert os.path.isdir(os.path.join(root, "user_sessions", "localhost-admin"))
    # Paths written with the old name - a runner's script, a desktop link - still work.
    assert old.is_symlink() and (old / "users.json").is_file()


def test_an_old_folder_that_cannot_be_linked_stays_in_use(frozen, monkeypatch):
    # Moved without a way back, every absolute path into it would break.
    bundle, home = frozen
    old = home / rp.LEGACY_USER_DIR_NAME
    old.mkdir()
    (old / "users.json").write_text("{}", encoding="utf-8")

    def refuse(target, link):
        raise OSError("no links here")

    monkeypatch.setattr(rp, "_link_directory", refuse)
    assert rp.ensure_user_data_root() == str(old)
    assert (old / "users.json").is_file()
    assert not (home / rp.USER_DIR_NAME).exists()


def test_cms_home_never_moves_the_old_folder(frozen, tmp_path, monkeypatch):
    bundle, home = frozen
    old = home / rp.LEGACY_USER_DIR_NAME
    old.mkdir()
    monkeypatch.setenv(rp.HOME_ENV, str(tmp_path / "scratch"))
    rp.ensure_user_data_root()
    assert old.is_dir() and not old.is_symlink()
    assert not (home / rp.USER_DIR_NAME).exists()


def test_a_checkout_never_moves_anything(tmp_path, monkeypatch):
    # Its data root is the checkout; the home folder is the GUI's to move.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv(rp.HOME_ENV, raising=False)
    monkeypatch.setattr(rp, "FROZEN", False)
    monkeypatch.setattr(rp, "app_root", lambda: str(tmp_path / "checkout"))
    (tmp_path / rp.LEGACY_USER_DIR_NAME).mkdir()
    rp.ensure_user_data_root()
    assert not (tmp_path / rp.USER_DIR_NAME).exists()


# ------------------------------------------------------ handing off to Chrome

def test_clean_env_restores_the_library_path_pyinstaller_replaced(monkeypatch):
    # Chrome must not load the bundle's libssl. The bootloader stashes the real
    # value under _ORIG precisely so it can be put back for a child like this.
    monkeypatch.setattr(rp, "FROZEN", True)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/app/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/mine")
    env = rp.clean_subprocess_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib/mine"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_clean_env_drops_the_library_path_when_there_was_none(monkeypatch):
    monkeypatch.setattr(rp, "FROZEN", True)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/app/_internal")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in rp.clean_subprocess_env()


def test_clean_env_leaves_a_checkout_alone(monkeypatch):
    monkeypatch.setattr(rp, "FROZEN", False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/lib/mine")
    assert rp.clean_subprocess_env()["LD_LIBRARY_PATH"] == "/usr/lib/mine"


# ------------------------------------------------------- what the bundle ships

def _gui_spec():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "packaging", "pyinstaller", "gui.spec"),
              encoding="utf-8") as fh:
        return fh.read()


def test_the_gui_bundle_ships_its_assets():
    """PyInstaller follows imports and nothing else.

    A file the app opens by path is simply absent from the bundle unless the
    spec names it - which is what nearly shipped a build with no splash: the app
    looked for the artwork, found nothing, and started straight into the main
    window. Nothing in the app's own tests could catch that, because in a
    checkout the file is right there.
    """
    spec = _gui_spec()
    assert "datas=datas" in spec, "the spec must pass its datas to Analysis"
    assert '"cms_gui", "assets"' in spec, "assets/ must be bundled"


def test_the_assets_the_gui_reads_at_runtime_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "gui", "cms_gui", "assets")
    assert os.path.isdir(assets)
    # At least one splash the loader will accept, or there is nothing to bundle.
    assert any(name.startswith("splash.") for name in os.listdir(assets))


def test_the_frozen_health_check_names_what_is_wrong():
    """Both builds call this; it is the one place that judges a bundle."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"))
    import check_frozen

    healthy = {"version": "1.2.3", "scenarios": [{"id": "a"}],
               "extensions": [{"name": "x"}], "warnings": []}
    assert check_frozen.problems(healthy, "1.2.3") == []

    # The two failures only the person who installs the package would see.
    assert any("did not make it into the bundle" in p for p in
               check_frozen.problems(dict(healthy, scenarios=[]), "1.2.3"))
    assert any("no extensions" in p for p in
               check_frozen.problems(dict(healthy, extensions=[]), "1.2.3"))
    # A bundle stamped with the wrong version is a build that used a stale tree.
    assert any("expected '9.9.9'" in p for p in
               check_frozen.problems(healthy, "9.9.9"))
    # And anything the core itself called unavailable.
    assert check_frozen.problems(
        dict(healthy, warnings=["playwright unavailable"]), "1.2.3") == \
        ["playwright unavailable"]


def test_cycle_runs_can_be_sent_elsewhere_and_back(tmp_path):
    default = rp.cycle_runs_dir()
    try:
        rp.set_cycle_runs_dir(str(tmp_path / "runs"))
        assert rp.cycle_runs_dir() == str(tmp_path / "runs")
    finally:
        rp.set_cycle_runs_dir("")
    assert rp.cycle_runs_dir() == default
