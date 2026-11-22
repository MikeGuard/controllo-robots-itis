"""Multi-file projects in the simulator behave like the robot run: output, errors and imports from every file."""

MAIN = "import Movimenti\nprint('main: before')\nMovimenti.pippo(5)\nprint('main: after')\n"
MOV = "def pippo(a):\n    print('ciao:', a)\n"


def _simulate(client, files, entry="main.py"):
    r = client.post("/api/simulate", json={"code": files[entry], "files": files, "entry_point": entry, "robot_model": "ur"})
    assert r.status_code == 200, r.text
    return r.json()


def test_print_inside_another_file_is_shown_in_order(client):
    """The reported bug: main.py calls Movimenti.pippo(5) and the print inside pippo never showed up."""
    res = _simulate(client, {"main.py": MAIN, "Movimenti.py": MOV})
    assert res["success"] is True
    assert res["logs"] == ["main: before", "ciao: 5", "main: after"]


def test_error_inside_another_file_names_that_file(client):
    res = _simulate(client, {"main.py": "import Movimenti\nMovimenti.pippo(0)\n",
                             "Movimenti.py": "def pippo(a):\n    print('dividing')\n    return 1 / a\n"})
    assert res["success"] is False
    assert res["error"] == "division by zero (Movimenti.py line 3, called from line 2)"
    assert res["logs"] == ["dividing"]                        # output printed before the error is kept


def test_missing_argument_is_a_runtime_error_with_the_call_line(client):
    res = _simulate(client, {"main.py": MAIN.replace("pippo(5)", "pippo()"), "Movimenti.py": MOV})
    assert res["success"] is False
    assert res["error"] == "pippo() missing 1 required positional argument: 'a' (line 3)"


def test_nested_package_printf_and_print_options_work_from_any_file(client):
    files = {"main.py": "from lib.util import f\nf()\n", "lib/__init__.py": "",
             "lib/util.py": "def f():\n    printf('%d-%s', 7, 'ok')\n    print('a', 'b', sep='+', end='!')\n    print(' end')\n"}
    res = _simulate(client, files)
    assert res["success"] is True, res.get("error")
    assert res["logs"] == ["7-ok", "a+b! end"]


def test_print_to_a_file_from_another_file_writes_the_file(client):
    files = {"main.py": "import helper\nhelper.save()\n",
             "helper.py": "def save():\n    with open('out.txt', 'w') as f:\n        print('x=1', file=f)\n    print('saved')\n"}
    res = _simulate(client, files)
    assert res["logs"] == ["saved"]
    assert res["output_files"] == {"out.txt": "x=1\n"}


def test_robot_commands_from_another_file_are_simulated(client):
    files = {"main.py": "from rtde_control import RTDEControlInterface as C\nimport moves\nmoves.go(C('1.2.3.4'))\n",
             "moves.py": "def go(c):\n    print('moving')\n    c.moveJ([0.5, -1.2, 1.0, -1.5, -1.5, 0.0], 0.5, 0.5)\n"}
    res = _simulate(client, files)
    assert res["success"] is True, res.get("error")
    assert res["logs"] == ["moving"]
    assert len(res["waypoints"]) > 2                          # the motion commanded in moves.py is in the trajectory


def test_every_file_the_program_writes_is_handed_back(client):
    """A file the program keeps overwriting must come back every time, even when its text is unchanged."""
    files = {"main.py": "import w\nw.go()\n", "same.txt": "unchanged",
             "w.py": "def go():\n"
                     "    open('same.txt', 'w').write('unchanged')\n"
                     "    open('data/log.txt', 'w').write('nested')\n"
                     "    open('blob.dat', 'w').write('x')\n"}
    res = _simulate(client, files)
    assert res["success"] is True, res.get("error")
    assert res["output_files"] == {"same.txt": "unchanged", "data/log.txt": "nested"}
    assert res["logs"] == ["[SANDBOX I/O] Not kept (file type not allowed in a project): blob.dat"]
