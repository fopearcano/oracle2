"""Tests for building apps and updating them from git. Run with: python3 -m unittest discover tests"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_oracle import TempDirCase, free_port, opener, oracle, wait_for, write  # noqa: E402

HAS_NODE = bool(shutil.which("node") and shutil.which("npm"))
HAS_GIT = bool(shutil.which("git"))


def g(cwd, *args):
    """Run git for a test, with an identity and no signing."""
    cmd = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=false"] + list(args)
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stdout}{r.stderr}")
    return r.stdout.strip()


def commit(repo, files: dict, message="change"):
    for name, text in files.items():
        write(Path(repo) / name, text)
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", message)
    return g(repo, "rev-parse", "HEAD")


def pkg(**fields):
    return json.dumps(fields)


# --------------------------------------------------------------- detection

class DetectBuildTest(TempDirCase):
    def app(self, name="app", package=None, files=None):
        folder = self.db / name
        write(folder / "package.json", json.dumps(package or {}))
        for rel, text in (files or {}).items():
            write(folder / rel, text)
        return folder

    def test_vite_app_is_built_and_its_output_served_not_its_source_index(self):
        folder = self.app(package={"scripts": {"dev": "vite", "build": "tsc && vite build"},
                                   "devDependencies": {"vite": "^5", "typescript": "^5"}},
                          files={"index.html": "<script type=module src=/src/main.ts></script>",
                                 "vite.config.ts": "export default { server: { port: 5273 }, build: { outDir: 'www' } }"})
        spec = oracle.detect(folder)
        self.assertEqual((spec["kind"], spec["root"], spec["build"], spec["framework"], spec["port"]),
                         ("static", "www", True, "vite", 5273))
        a = oracle.scan(self.db, {})["app"]
        self.assertIsNone(a.problem)                 # www/ doesn't exist yet: the build makes it
        self.assertEqual(oracle.required_files(a), ["package.json"])

    def test_vite_preview_port_wins_and_output_defaults_to_dist(self):
        folder = self.app(package={"scripts": {"build": "vite build", "start": "vite"}, "devDependencies": {"vite": "^5"}},
                          files={"vite.config.js": "export default { server: { port: 5000 }, preview: { host: true, port: 5333 } }"})
        spec = oracle.detect(folder)
        self.assertEqual((spec["kind"], spec["root"], spec["port"]), ("static", "dist", 5333))

    def test_static_frameworks(self):
        cases = [
            ({"react-scripts": "5"}, {}, ("static", "build", "create-react-app")),
            ({"astro": "4"}, {}, ("static", "dist", "astro")),
            ({"gatsby": "5"}, {}, ("static", "public", "gatsby")),
            ({"@vue/cli-service": "5"}, {}, ("static", "dist", "vue-cli")),
            ({"next": "14"}, {"next.config.mjs": "export default { output: 'export' }"}, ("static", "out", "next")),
            ({"@sveltejs/kit": "2", "@sveltejs/adapter-static": "3"}, {}, ("static", "build", "sveltekit")),
            ({"@angular/core": "17"}, {}, ("static", None, "angular")),
        ]
        for i, (deps, files, expected) in enumerate(cases):
            folder = self.app(f"app{i}", {"scripts": {"build": "x"}, "dependencies": deps}, files)
            spec = oracle.detect(folder)
            self.assertEqual((spec["kind"], spec["root"], spec["framework"]), expected, deps)

    def test_nuxt_generate_is_static(self):
        folder = self.app(package={"scripts": {"build": "nuxt generate"}, "devDependencies": {"nuxt": "3"}})
        self.assertEqual(oracle.detect(folder)["root"], ".output/public")

    def test_server_frameworks(self):
        cases = [
            ({"next": "14"}, "next"), ({"nuxt": "3"}, "nuxt"),
            ({"@sveltejs/kit": "2", "@sveltejs/adapter-node": "5"}, "sveltekit"),
            ({"astro": "4", "@astrojs/node": "8"}, "astro"),
            ({"@remix-run/serve": "2", "@remix-run/dev": "2"}, "remix"),
            ({"@react-router/serve": "7"}, "react-router"),
        ]
        for i, (deps, framework) in enumerate(cases):
            folder = self.app(f"app{i}", {"scripts": {"build": "x", "start": "x start"}, "dependencies": deps})
            spec = oracle.detect(folder)
            self.assertEqual((spec["kind"], spec["framework"], spec["build"]), ("server", framework, True), deps)

    def test_sveltekit_without_a_local_adapter_says_what_to_change(self):
        self.app(package={"scripts": {"build": "vite build"}, "devDependencies": {"@sveltejs/kit": "2",
                                                                                 "@sveltejs/adapter-auto": "3"}})
        problem = oracle.scan(self.db, {})["app"].problem
        self.assertIn("adapter-node", problem)

    def test_own_server_is_built_then_started(self):
        folder = self.app(package={"scripts": {"build": "tsc", "start": "node dist/server.js"}},
                          files={"dist/server.js": "const port = process.env.PORT || 3300;"})
        spec = oracle.detect(folder)
        self.assertEqual((spec["kind"], spec["entry"], spec["build"], spec["port"]), ("node", "dist/server.js", True, 3300))
        folder = self.app("b", package={"scripts": {"build": "npm run build -w web", "start": "npm run start -w server"}})
        spec = oracle.detect(folder)
        self.assertEqual((spec["kind"], spec["build"]), ("npm", True))

    def test_apps_without_a_build_script_are_detected_as_before(self):
        folder = self.app(package={"scripts": {"start": "python3 -m http.server 8010"}}, files={"index.html": ""})
        self.assertEqual(oracle.detect(folder), {"kind": "static", "port": 8010})

    def test_registry_can_turn_the_build_off_or_choose_the_output(self):
        self.app(package={"scripts": {"build": "vite build"}, "devDependencies": {"vite": "5"}}, files={"index.html": ""})
        registry = {"app": {"name": "app", "kind": "static", "build": False, "root": ".", "port": 8123}}
        a = oracle.scan(self.db, registry)["app"]
        self.assertFalse(a.wants_build)
        self.assertEqual(oracle.static_root(a), ".")


class StaticRootTest(TempDirCase):
    def atlas(self, spec):
        write(self.db / "app" / "package.json", pkg(scripts={"build": "x"}))
        return oracle.scan(self.db, {"app": dict(spec, name="app")})["app"]

    def test_output_is_found_after_the_build(self):
        a = self.atlas({"kind": "static", "build": True, "framework": "angular"})
        self.assertIsNone(oracle.static_root(a))
        write(self.db / "app" / "dist" / "my-app" / "browser" / "index.html", "")
        self.assertEqual(oracle.static_root(a), "dist/my-app/browser")

    def test_a_build_that_only_makes_data_serves_the_folder(self):
        a = self.atlas({"kind": "static", "build": True})
        write(self.db / "app" / "index.html", "")
        self.assertEqual(oracle.static_root(a), ".")
        write(self.db / "app" / "out" / "index.html", "")
        self.assertEqual(oracle.static_root(a), "out")


# --------------------------------------------------------- package managers

class PackageManagerTest(TempDirCase):
    def test_lockfile_decides(self):
        app = self.db / "app"
        write(app / "package.json", "{}")
        self.assertEqual(oracle.package_manager(app), ("npm", None))
        write(app / "yarn.lock", "")
        self.assertEqual(oracle.package_manager(app), ("yarn", "yarn.lock"))
        write(app / "pnpm-lock.yaml", "")
        self.assertEqual(oracle.package_manager(app), ("pnpm", "pnpm-lock.yaml"))
        write(app / "package-lock.json", "{}")
        self.assertEqual(oracle.package_manager(app), ("npm", "package-lock.json"))
        write(self.db / "b" / "package.json", pkg(packageManager="pnpm@9.1.0"))
        self.assertEqual(oracle.package_manager(self.db / "b"), ("pnpm", None))

    def test_install_commands_never_rewrite_the_lockfile(self):
        app = self.db / "app"
        write(app / "package.json", "{}")
        write(app / "package-lock.json", "{}")
        first, fallback = oracle.install_commands(app, "npm", ["npm"])
        self.assertEqual(first[:3], ["npm", "ci", "--include=dev"])
        self.assertIn("--no-save", fallback)
        (app / "package-lock.json").unlink()
        self.assertEqual(oracle.install_commands(app, "npm", ["npm"])[0][:3], ["npm", "install", "--no-save"])
        self.assertEqual(oracle.install_commands(app, "pnpm", ["corepack", "pnpm"])[0],
                         ["corepack", "pnpm", "install", "--frozen-lockfile"])
        write(app / "yarn.lock", "# yarn lockfile v1\n")
        self.assertIn("--frozen-lockfile", oracle.install_commands(app, "yarn", ["yarn"])[0])
        write(app / ".yarnrc.yml", "nodeLinker: node-modules\n")
        self.assertEqual(oracle.install_commands(app, "yarn", ["yarn"])[0], ["yarn", "install", "--immutable"])

    def test_pnpm_and_yarn_go_through_corepack(self):
        app = self.db / "app"
        write(app / "package.json", "{}")
        write(app / "pnpm-lock.yaml", "")
        paths = {"corepack": "/usr/bin/corepack", "pnpm": "/usr/bin/pnpm"}
        with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: paths.get(n)):
            self.assertEqual(oracle.pm_program(app), ("pnpm", ["/usr/bin/corepack", "pnpm"]))
        paths.pop("corepack")
        with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: paths.get(n)):
            self.assertEqual(oracle.pm_program(app), ("pnpm", ["/usr/bin/pnpm"]))
        paths.pop("pnpm")
        with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: paths.get(n)):
            with self.assertRaises(oracle.LaunchError) as e:
                oracle.pm_program(app)
        self.assertIn("corepack enable", str(e.exception))
        self.assertIn("pnpm-lock.yaml", str(e.exception))

    def test_bun_is_not_supported_and_says_so(self):
        write(self.db / "app" / "bun.lockb", "")
        with self.assertRaises(oracle.LaunchError) as e:
            oracle.pm_program(self.db / "app")
        self.assertIn("doesn't support", str(e.exception))

    def test_install_and_build_never_run_as_production(self):
        env = oracle.build_env({"NODE_ENV": "production", "PATH": "x"})
        self.assertNotIn("NODE_ENV", env)
        self.assertEqual(env["COREPACK_ENABLE_DOWNLOAD_PROMPT"], "0")

    def test_dev_dependencies_and_workspaces_need_an_install(self):
        app = self.db / "app"
        write(app / "package.json", pkg(devDependencies={"vite": "^5"}))
        self.assertTrue(oracle.needs_install(app))
        write(app / "node_modules" / ".package-lock.json", "{}")
        future = time.time() + 10
        os.utime(app / "node_modules" / ".package-lock.json", (future, future))
        self.assertFalse(oracle.needs_install(app))
        write(app / "package-lock.json", "{}")                 # a pulled lockfile is newer
        os.utime(app / "package-lock.json", (future + 5, future + 5))
        self.assertTrue(oracle.needs_install(app))
        oracle.mark_installed(app)                             # Oracle's own marker, written after an install
        os.utime(app / "node_modules" / ".oracle-installed", (future + 9, future + 9))
        self.assertFalse(oracle.needs_install(app))
        write(self.db / "ws" / "package.json", pkg(workspaces=["web", "server"]))
        self.assertTrue(oracle.needs_install(self.db / "ws"))


# -------------------------------------------------------------- build stamps

@unittest.skipUnless(HAS_GIT, "needs git")
class BuildStampTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = self.db / "app"
        self.app.mkdir()
        g(self.app, "init", "-q")
        commit(self.app, {"package.json": pkg(scripts={"build": "node build.js"}), "src.txt": "one",
                          ".gitignore": "dist/\nnode_modules/\n"})
        self.cmd = ["/opt/node/bin/npm", "run", "build"]

    def atlas(self, **spec):
        base = {"name": "app", "kind": "static", "build": True, "root": "dist", "port": 8123}
        return oracle.scan(self.db, {"app": dict(base, **spec)})["app"]

    def built(self, a):
        write(self.app / "dist" / "index.html", "built")
        oracle.save_stamp(a, self.cmd)

    def test_rebuild_only_when_something_changed(self):
        a = self.atlas()
        self.assertEqual(oracle.build_reason(a, self.cmd), "first build")
        self.built(a)
        self.assertIsNone(oracle.build_reason(a, self.cmd))
        self.assertTrue(oracle.stamp_path("app").is_file())
        self.assertTrue(str(oracle.stamp_path("app")).startswith(str(self.tmp / "state")))   # not in the app
        self.assertEqual(g(self.app, "status", "--porcelain"), "")

        shutil.rmtree(self.app / "dist")
        self.assertEqual(oracle.build_reason(a, self.cmd), "the build output is missing")
        self.built(a)

        commit(self.app, {"src.txt": "two"})
        self.assertIn("new code", oracle.build_reason(self.atlas(), self.cmd))
        self.built(a)
        self.assertIsNone(oracle.build_reason(a, self.cmd))

        write(self.app / "src.txt", "edited, not committed")
        self.assertEqual(oracle.build_reason(a, self.cmd), "the code changed since the last build")
        self.built(a)
        self.assertIsNone(oracle.build_reason(a, self.cmd))

        self.assertEqual(oracle.build_reason(a, ["npm", "run", "build:prod"]), "the build command changed")
        # where npm lives doesn't matter
        self.assertIsNone(oracle.build_reason(a, ["C:\\Program Files\\nodejs\\npm.cmd", "run", "build"]))

    def test_lockfile_change_rebuilds(self):
        commit(self.app, {".gitignore": "dist/\nnode_modules/\npackage-lock.json\n"})
        write(self.app / "package-lock.json", '{"v": 1}')      # ignored here, so only its hash changes
        a = self.atlas()
        self.built(a)
        self.assertIsNone(oracle.build_reason(a, self.cmd))
        write(self.app / "package-lock.json", '{"v": 2}')
        self.assertEqual(oracle.build_reason(a, self.cmd), "the dependencies changed")

    def test_a_built_static_site_needs_no_install_until_it_is_rebuilt(self):
        commit(self.app, {"package.json": pkg(scripts={"build": "vite build"}, devDependencies={"vite": "5"})})
        a = self.atlas()
        with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: f"/bin/{n}"), \
                mock.patch.object(oracle, "engine_problem", return_value=None):
            prep = oracle.Runner(a, "127.0.0.1").prepare()
            self.assertEqual((prep.reason, len(prep.installs)), ("first build", 1))   # npm install --no-save
            self.built(a)
            prep = oracle.Runner(a, "127.0.0.1").prepare()                         # node_modules is still missing
            self.assertEqual((prep.reason, prep.installs), (None, []))
            server = self.atlas(kind="server", framework="remix", root=None)
            self.assertTrue(oracle.Runner(server, "127.0.0.1").prepare().installs)  # servers need node_modules

    def test_folder_without_git_uses_its_files(self):
        shutil.rmtree(self.app / ".git")
        a = self.atlas()
        self.built(a)
        self.assertIsNone(oracle.build_reason(a, self.cmd))
        time.sleep(0.01)
        write(self.app / "src.txt", "a longer text than before")
        self.assertEqual(oracle.build_reason(self.atlas(), self.cmd), "the code changed since the last build")


# ------------------------------------------------------------- SPA fallback

class SpaFallbackTest(TempDirCase):
    def test_which_paths_fall_back(self):
        root = self.db / "dist"
        write(root / "index.html", "app")
        write(root / "assets" / "app.js", "")

        def fb(path):
            return oracle.spa_fallback(path, str(root / path.lstrip("/").split("?")[0]))
        self.assertTrue(fb("/notes/42"))
        self.assertTrue(fb("/notes/42/?tab=x"))
        self.assertTrue(fb("/settings/"))
        self.assertFalse(fb("/assets/app.js"))           # exists
        self.assertFalse(fb("/assets/missing.js"))       # a file that isn't there stays a 404
        self.assertTrue(fb("/v1.2/missing"))             # only the last part counts
        self.assertFalse(fb("/assets"))                  # an existing folder

    def serve(self, spa):
        quiet = mock.patch.object(oracle.StaticHandler, "log_request", lambda *args, **kw: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        root = self.db / f"dist-{spa}"
        write(root / "index.html", "<title>app</title>")
        write(root / "assets" / "app.js", "export {}")
        handler = oracle.functools.partial(oracle.SpaHandler if spa else oracle.StaticHandler, directory=str(root))
        httpd = oracle.Server(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def get(self, port, path):
        try:
            with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                return resp.status, resp.read()
        except Exception as e:
            return getattr(e, "code", None), b""

    def test_client_routes_survive_a_reload(self):
        port = self.serve(spa=True)
        self.assertEqual(self.get(port, "/notes/42"), (200, b"<title>app</title>"))
        self.assertEqual(self.get(port, "/assets/app.js"), (200, b"export {}"))
        self.assertEqual(self.get(port, "/assets/nope.js")[0], 404)
        plain = self.serve(spa=False)
        self.assertEqual(self.get(plain, "/notes/42")[0], 404)       # plain sites are unchanged

    def test_built_static_apps_are_served_with_the_fallback(self):
        write(self.db / "app" / "package.json", pkg(scripts={"build": "x"}))
        write(self.db / "app" / "dist" / "index.html", "")
        a = oracle.scan(self.db, {"app": {"name": "app", "kind": "static", "build": True, "root": "dist", "port": 8123}})["app"]
        cmd, _ = oracle.Runner(a, "127.0.0.1").command()
        self.assertIn("--spa", cmd)
        self.assertTrue(cmd[cmd.index("serve-static") + 1].endswith("dist"))


# ---------------------------------------------------------------- build queue

class BuildQueueTest(unittest.TestCase):
    def test_one_at_a_time_in_arrival_order(self):
        q = oracle.BuildQueue()
        order, busy, overlap = [], [0], []
        lock = threading.Lock()

        def job(name):
            self.assertTrue(q.acquire(name))
            with lock:
                busy[0] += 1
                overlap.append(busy[0])
                order.append(name)
            time.sleep(0.05)
            with lock:
                busy[0] -= 1
            q.release(name)
        threads = []
        for name in "abcde":
            t = threading.Thread(target=job, args=(name,))
            t.start()
            threads.append(t)
            time.sleep(0.02)            # arrive in this order
        for t in threads:
            t.join(5)
        self.assertEqual(max(overlap), 1)
        self.assertEqual(order, list("abcde"))

    def test_a_waiting_app_can_give_up(self):
        q = oracle.BuildQueue()
        self.assertTrue(q.acquire("first"))
        cancel = threading.Event()
        waited, result = threading.Event(), []
        t = threading.Thread(target=lambda: result.append(q.acquire("second", cancel.is_set, waited.set)))
        t.start()
        self.assertTrue(waited.wait(2))                 # told that it has to wait
        third = []
        t3 = threading.Thread(target=lambda: third.append(q.acquire("third")))
        t3.start()
        time.sleep(0.1)
        cancel.set()
        t.join(2)
        self.assertEqual(result, [False])
        q.release("first")
        t3.join(2)
        self.assertEqual(third, [True])                 # the next in line goes on


# ------------------------------------------------------- runners that build

def build_app(folder: Path, delay: float = 0, fail: bool = False, shared_log: Path = None):
    """An app whose build (node build.js) copies src.txt into dist/index.html."""
    write(folder / "package.json", pkg(name=folder.name, scripts={"build": "node build.js"}))
    write(folder / "src.txt", "version one")
    log = json.dumps(str(shared_log)) if shared_log else "null"
    write(folder / "build.js", f"""
const fs = require('fs');
const log = {log};
if (log) fs.appendFileSync(log, 'start {folder.name} ' + Date.now() + '\\n');
setTimeout(() => {{
  if ({'true' if fail else 'false'}) {{ console.error('the build broke'); process.exit(2); }}
  fs.mkdirSync('dist', {{ recursive: true }});
  fs.writeFileSync('dist/index.html', fs.readFileSync('src.txt', 'utf8') + ' NODE_ENV=' + process.env.NODE_ENV);
  fs.appendFileSync('builds.txt', 'x');
  if (log) fs.appendFileSync(log, 'end {folder.name} ' + Date.now() + '\\n');
}}, {int(delay * 1000)});
""")
    write(folder / ".gitignore", "dist/\nbuilds.txt\n")


def builds(folder: Path) -> int:
    try:
        return len((folder / "builds.txt").read_text())
    except OSError:
        return 0


@unittest.skipUnless(HAS_NODE and HAS_GIT, "needs Node.js, npm and git")
class RunnerBuildTest(TempDirCase):
    def registry(self, name, **spec):
        return {name: dict({"name": name, "kind": "static", "build": True, "root": "dist", "port": free_port()}, **spec)}

    def runner(self, name, registry=None, queue=None):
        a = oracle.scan(self.db, registry if registry is not None else self.registry(name))[name]
        states = []
        r = oracle.Runner(a, "127.0.0.1", on_change=lambda r: states.append(r.state), queue=queue or oracle.BuildQueue())
        self.addCleanup(r.stop)
        return r, states

    def page(self, r):
        with opener.open(f"http://127.0.0.1:{r.atlas.port}/some/route", timeout=5) as resp:
            return resp.read().decode()

    def test_builds_once_then_starts_at_once_until_the_code_changes(self):
        app = self.db / "app"
        build_app(app)
        g(app, "init", "-q")
        commit(app, {})
        r, states = self.runner("app")
        with mock.patch.dict(os.environ, {"NODE_ENV": "production"}):
            r.start()
            self.assertTrue(wait_for(lambda: r.state == "running", 60), r.message)
        self.assertIn("building", states)
        self.assertEqual(self.page(r), "version one NODE_ENV=undefined")     # never production for a build
        self.assertIn("$ npm run build", (self.tmp / "logs" / "app.log").read_text())
        r.stop()

        r.start()                                                        # nothing changed: no build
        self.assertTrue(wait_for(lambda: r.state == "running", 30), r.message)
        self.assertEqual(builds(app), 1)
        r.stop()

        commit(app, {"src.txt": "version two"})                          # what an Update brings in
        r.atlas = oracle.scan(self.db, {"app": dict(self.registry("app")["app"], port=r.atlas.port)})["app"]
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "running", 60), r.message)
        self.assertEqual(builds(app), 2)
        self.assertTrue(self.page(r).startswith("version two"))
        self.assertIn("new code", (self.tmp / "logs" / "app.log").read_text())

    def test_failed_build_says_so_and_can_be_retried(self):
        app = self.db / "app"
        build_app(app, fail=True)
        r, _ = self.runner("app")
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "failed", 60), r.message)
        self.assertEqual(r.message, "Build failed: open the log")
        self.assertIn("the build broke", oracle.tail(r.log_path))
        self.assertIsNone(oracle.read_stamp("app"))
        build_app(app)                                                   # fixed
        r.start(manual=True)                                             # Retry
        self.assertTrue(wait_for(lambda: r.state == "running", 60), r.message)

    def test_found_apps_build_only_after_a_start_by_hand(self):
        app = self.db / "found"
        build_app(app)
        r, states = self.runner("found", registry={})
        self.assertFalse(r.atlas.known)
        r.start()                                                        # e.g. autostart at login
        self.assertTrue(wait_for(lambda: r.state == "untrusted", 20), r.state)
        self.assertEqual(builds(app), 0)
        self.assertIn("start it once by hand", r.message.lower())
        r.start(manual=True)                                             # the Start button, or --only
        self.assertTrue(wait_for(lambda: r.state == "running", 60), r.message)
        self.assertEqual(builds(app), 1)
        self.assertTrue(oracle.is_trusted(r.atlas))
        r.stop()
        write(app / "src.txt", "changed")                                # needs a rebuild...
        r.start()                                                        # ...which is allowed now
        self.assertTrue(wait_for(lambda: r.state == "running", 60), r.message)
        self.assertEqual(builds(app), 2)

    def test_one_build_at_a_time_and_the_rest_wait(self):
        shared = self.tmp / "builds.log"
        queue = oracle.BuildQueue()
        runners = []
        for name in ("one", "two", "three"):
            build_app(self.db / name, delay=0.8, shared_log=shared)
        registry = {}
        for name in ("one", "two", "three"):
            registry.update(self.registry(name))
        for name in ("one", "two", "three"):
            runners.append(self.runner(name, registry, queue))
        for r, _ in runners:
            r.start()
        self.assertTrue(wait_for(lambda: all(r.state == "running" for r, _ in runners), 90),
                        [(r.state, r.message) for r, _ in runners])
        self.assertTrue(any("queued" in states for _, states in runners))
        events = [line.split() for line in shared.read_text().splitlines()]
        self.assertEqual([e[0] for e in events], ["start", "end"] * 3)     # never two at once

    def test_stopping_an_app_that_waits_to_build(self):
        queue = oracle.BuildQueue()
        queue.acquire("someone else")
        build_app(self.db / "app")
        r, _ = self.runner("app", queue=queue)
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "queued", 20), r.state)
        self.assertIn("waiting for another app", r.message)
        self.assertIsNotNone(r.info(True)["elapsed"])
        r.stop()
        self.assertEqual(r.state, "stopped")
        queue.release("someone else")
        time.sleep(1)
        self.assertEqual(builds(self.db / "app"), 0)


class ServerCommandTest(TempDirCase):
    """How framework servers get Oracle's port and host; LAN mode passes 0.0.0.0."""

    def atlas(self, framework, **extra):
        write(self.db / "app" / "package.json", pkg(scripts={"build": "x", "start": "x"}))
        spec = dict({"name": "app", "kind": "server", "framework": framework, "port": 4567}, **extra)
        return oracle.scan(self.db, {"app": spec})["app"]

    def test_next_gets_port_and_hostname_flags(self):
        a = self.atlas("next")
        write(self.db / "app" / "node_modules" / "next" / "package.json", pkg(bin={"next": "dist/bin/next"}))
        write(self.db / "app" / "node_modules" / "next" / "dist" / "bin" / "next", "")
        for host in ("127.0.0.1", "0.0.0.0"):
            cmd, env = oracle.server_command(a, host, "node")
            self.assertEqual(cmd[0], "node")
            self.assertTrue(cmd[1].endswith(os.path.join("next", "dist", "bin", "next")))
            self.assertEqual(cmd[2:], ["start", "-p", "4567", "-H", host])

    def test_nuxt_sveltekit_astro(self):
        cmd, env = oracle.server_command(self.atlas("nuxt"), "0.0.0.0", "node")
        self.assertEqual((cmd[1], env["NITRO_PORT"], env["NITRO_HOST"]), (".output/server/index.mjs", "4567", "0.0.0.0"))
        cmd, env = oracle.server_command(self.atlas("sveltekit"), "127.0.0.1", "node")
        self.assertEqual(cmd[1], "build/index.js")
        cmd, _ = oracle.server_command(self.atlas("astro"), "127.0.0.1", "node")
        self.assertEqual(cmd[1], "dist/server/entry.mjs")
        self.assertIsNone(oracle.server_command(self.atlas("remix"), "127.0.0.1", "node"))   # npm start

    def test_every_kind_gets_port_host_and_its_own_variables(self):
        a = self.atlas("remix", env={"PERSONA_PORT": "{port}", "PERSONA_HOST": "{host}"})
        for host in ("127.0.0.1", "0.0.0.0"):
            r = oracle.Runner(a, host)
            with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: f"/bin/{n}"):
                cmd, env = r.command()
            self.assertEqual(cmd, ["/bin/npm", "start"])
            self.assertEqual((env["PORT"], env["HOST"], env["PERSONA_PORT"], env["PERSONA_HOST"]),
                             ("4567", host, "4567", host))
        write(self.db / "site" / "index.html", "")
        site = oracle.scan(self.db, {"site": {"name": "site", "kind": "static", "port": 4568}})["site"]
        cmd, _ = oracle.Runner(site, "0.0.0.0").command()
        self.assertEqual(cmd[cmd.index("--host") + 1], "0.0.0.0")
        self.assertNotIn("--spa", cmd)


# --------------------------------------------------------- branches and sync

SAMPLE_LS_REMOTE = """ref: refs/heads/main\tHEAD
1111111111111111111111111111111111111111\tHEAD
2222222222222222222222222222222222222222\trefs/heads/claude/app
"""


class BranchChoiceTest(unittest.TestCase):
    def test_parse_ls_remote(self):
        info = oracle.parse_ls_remote(SAMPLE_LS_REMOTE)
        self.assertEqual(info["default"], "main")
        self.assertEqual(info["heads"], {"main": "1" * 40, "claude/app": "2" * 40})

    def test_choose_branch(self):
        info = oracle.parse_ls_remote(SAMPLE_LS_REMOTE)
        self.assertEqual(oracle.choose_branch("claude/app", info), "claude/app")
        self.assertEqual(oracle.choose_branch("claude/gone", info), "main")      # deleted: the default
        self.assertEqual(oracle.choose_branch(None, info), "main")


@unittest.skipUnless(HAS_GIT, "needs git")
class SyncTest(TempDirCase):
    """A local repository plays GitHub."""

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        g(self.origin, "init", "-q")
        commit(self.origin, {"book.epub": "only the book"}, "book")             # main: no app
        g(self.origin, "checkout", "-q", "-b", "claude/app")
        self.v1 = commit(self.origin, {"index.html": "v1"}, "app")
        g(self.origin, "checkout", "-q", "main")
        self.clone = self.db / "app"
        g(self.tmp, "clone", "-q", str(self.origin), str(self.clone))             # on main, like story

    def push(self, files, branch="claude/app"):
        g(self.origin, "checkout", "-q", branch)
        sha = commit(self.origin, files)
        g(self.origin, "checkout", "-q", "main")
        return sha

    def head(self):
        return g(self.clone, "rev-parse", "HEAD")

    def branch(self):
        return g(self.clone, "rev-parse", "--abbrev-ref", "HEAD")

    def sync(self, wanted="claude/app", required=("index.html",)):
        return oracle.update_clone(self.clone, wanted, list(required))

    def test_switches_to_the_tracked_branch_then_fast_forwards(self):
        res = self.sync()
        self.assertEqual(res.status, "updated", res.text)
        self.assertEqual((self.branch(), self.head()), ("claude/app", self.v1))
        self.assertIn("switched from 'main' to 'claude/app'", res.text)
        res = self.sync()
        self.assertEqual(res.status, "uptodate")
        self.assertIn("already up to date (claude/app", res.text)
        v2 = self.push({"index.html": "v2"})
        res = self.sync()
        self.assertEqual((res.status, res.old, res.new), ("updated", self.v1, v2))
        self.assertEqual(res.text, f"updated {self.v1[:7]} -> {v2[:7]} (claude/app)")

    def test_uncommitted_changes_block_a_switch_and_change_nothing(self):
        write(self.clone / "book.epub", "my notes")
        before = self.head()
        res = self.sync()
        self.assertEqual(res.status, "skipped")
        self.assertIn("uncommitted changes to book.epub", res.text)
        self.assertEqual((self.branch(), self.head()), ("main", before))
        self.assertEqual((self.clone / "book.epub").read_text(), "my notes")

    def test_uncommitted_changes_in_the_way_of_a_fast_forward(self):
        self.sync()
        self.push({"index.html": "v2"})
        write(self.clone / "index.html", "edited here")
        res = self.sync()
        self.assertEqual(res.status, "skipped")
        self.assertIn("uncommitted changes to index.html are in the way", res.text)
        self.assertIn("checkout -- index.html", res.text)
        self.assertEqual((self.head(), (self.clone / "index.html").read_text()), (self.v1, "edited here"))

    def test_changes_that_are_not_in_the_way_are_kept(self):
        self.sync()
        write(self.clone / "notes.txt", "mine")
        v2 = self.push({"index.html": "v2"})
        self.assertEqual(self.sync().status, "updated")
        self.assertEqual(self.head(), v2)
        self.assertEqual((self.clone / "notes.txt").read_text(), "mine")

    def test_local_commits(self):
        self.sync()
        mine = commit(self.clone, {"local.txt": "x"}, "mine")
        res = self.sync()                                   # ahead only: nothing to get
        self.assertEqual(res.status, "uptodate")
        self.assertIn("1 local commit that isn't on GitHub", res.text)
        self.push({"index.html": "v2"})
        res = self.sync()                                   # diverged: in the way
        self.assertEqual(res.status, "skipped")
        self.assertIn("can't fast-forward", res.text)
        self.assertEqual(self.head(), mine)

    def test_a_deleted_branch_falls_back_to_the_default_but_never_to_one_without_the_app(self):
        self.sync()
        g(self.origin, "branch", "-q", "-D", "claude/app")
        res = self.sync()                                   # main has no index.html: stay
        self.assertEqual(res.status, "skipped")
        self.assertIn("'main' has no index.html", res.text)
        self.assertIn("gone from GitHub", res.text)
        self.assertEqual(self.branch(), "claude/app")
        merged = commit(self.origin, {"index.html": "merged into main"})     # the work lands on main
        res = self.sync()
        self.assertEqual(res.status, "updated", res.text)
        self.assertEqual((self.branch(), self.head()), ("main", merged))

    def test_follows_a_new_default_branch(self):
        g(self.origin, "checkout", "-q", "-b", "trunk", "claude/app")
        g(self.origin, "symbolic-ref", "HEAD", "refs/heads/trunk")             # "make trunk the default"
        res = self.sync(wanted=None)
        self.assertEqual(res.status, "updated", res.text)
        self.assertEqual(self.branch(), "trunk")

    def test_not_a_clone(self):
        write(self.db / "plain" / "index.html", "")
        self.assertEqual(oracle.update_clone(self.db / "plain").status, "ignored")

    def test_update_available(self):
        self.sync()
        self.assertFalse(oracle.update_status(self.clone, "claude/app")["available"])
        v2 = self.push({"index.html": "v2"})
        status = oracle.update_status(self.clone, "claude/app")
        self.assertTrue(status["available"])
        self.assertEqual((status["local"], status["remote"]), (self.v1[:7], v2[:7]))
        self.sync()
        self.assertFalse(oracle.update_status(self.clone, "claude/app")["available"])
        commit(self.clone, {"local.txt": "x"})                                  # ahead: nothing to get
        self.assertFalse(oracle.update_status(self.clone, "claude/app")["available"])
        g(self.clone, "checkout", "-q", "main")
        self.assertTrue(oracle.update_status(self.clone, "claude/app")["switch"])

    def test_sync_command_prints_one_line_per_app_including_found_clones(self):
        registry = {"app": {"name": "app", "kind": "static", "port": 8123, "branch": "claude/app"}}
        found = self.db / "found"
        g(self.tmp, "clone", "-q", str(self.origin), str(found))
        g(found, "checkout", "-q", "claude/app")
        g(found, "reset", "-q", "--hard", "HEAD")
        v2 = self.push({"index.html": "v2"})
        write(self.db / "plain" / "index.html", "")
        with mock.patch.object(oracle, "load_registry", return_value=registry), \
                mock.patch.object(oracle, "call_oracle", return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = oracle.cmd_sync(oracle.build_parser().parse_args(["sync", "--db", str(self.db)]))
        lines = [ln.split(None, 1) for ln in out.getvalue().splitlines() if ln.startswith("  ")]
        self.assertEqual(code, 1, out.getvalue())             # "found" could not be synced
        self.assertIn("1 app could not be synced", out.getvalue())
        result = dict(lines)
        self.assertEqual(set(result), {"app", "found", "plain"})
        self.assertTrue(result["app"].startswith("switched from 'main' to 'claude/app'"), result["app"])
        # an app Oracle found by itself follows its own origin's default branch... which has no app here
        self.assertIn("'main' has no index.html", result["found"])
        self.assertEqual(result["plain"], "skipped: not a git clone")
        self.assertEqual(g(self.clone, "rev-parse", "HEAD"), v2)


@unittest.skipUnless(HAS_GIT, "needs git")
class DashboardUpdateTest(TempDirCase):
    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin"
        origin.mkdir()
        g(origin, "init", "-q")
        self.v1 = commit(origin, {"index.html": "v1"})
        self.origin = origin
        g(self.tmp, "clone", "-q", str(origin), str(self.db / "site"))
        write(self.db / "plain" / "index.html", "")
        self.port, self.site_port = free_port(), free_port()
        registry = {"site": {"name": "site", "kind": "static", "port": self.site_port},
                    "plain": {"name": "plain", "kind": "static", "port": free_port()}}
        self.sup = oracle.Supervisor(self.db, registry, oracle.load_config(), dashboard_port=self.port, quiet=True)
        self.httpd = oracle.make_dashboard(self.sup, self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.sup.stop_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, header=True):
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST",
                      headers={"X-Oracle": "1"} if header else {})
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except Exception as e:
            return getattr(e, "code", None), None

    def site(self):
        return next(a for a in self.sup.status()["atlases"] if a["name"] == "site")

    def test_update_button_and_update_all(self):
        self.assertEqual(self.post("/api/atlas/site/update", header=False)[0], 403)
        self.assertEqual(self.post("/api/atlas/plain/update")[0], 409)          # not a clone
        self.assertEqual(self.post("/api/atlas/site/start")[0], 200)
        self.assertTrue(wait_for(lambda: self.site()["state"] == "running"))
        v2 = commit(self.origin, {"index.html": "v2"})
        self.assertEqual(self.post("/api/rescan")[0], 200)                       # asks GitHub
        self.assertTrue(wait_for(lambda: (self.site()["update"] or {}).get("available")))
        code, status = self.post("/api/atlas/site/update")
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: self.site()["note"].startswith("updated")), self.site())
        self.assertEqual(g(self.db / "site", "rev-parse", "HEAD"), v2)
        self.assertTrue(wait_for(lambda: self.site()["update"]["available"] is False))
        site = self.site()
        self.assertEqual(site["state"], "running")          # a plain static site isn't restarted
        with opener.open(f"http://127.0.0.1:{self.site_port}/", timeout=5) as resp:
            self.assertEqual(resp.read(), b"v2")            # ...and serves the new files at once
        self.assertEqual(self.post("/api/update-all")[0], 200)
        self.assertTrue(wait_for(lambda: self.site()["note"].startswith("already up to date")), self.site())

    def test_running_servers_restart_after_an_update(self):
        r = self.sup.runner("site")
        r.atlas.kind = "cmd"                    # pretend it is a server
        with mock.patch.object(r, "state", "running"), mock.patch.object(r, "proc", object()), \
                mock.patch.object(self.sup, "restart") as restart:
            commit(self.origin, {"index.html": "v2"})
            self.sup.update("site", wait=True)
        restart.assert_called_once_with("site")


@unittest.skipUnless(HAS_GIT, "needs git")
class LanUpdateTest(TempDirCase):
    """Another device on the network (a faked client address) and the new buttons."""

    def setUp(self):
        super().setUp()
        cfg = oracle.load_config()
        oracle.ensure_access_code(cfg)
        cfg["access_code"] = "k7mp-q2xa"
        oracle.save_config(cfg)
        origin = self.tmp / "origin"
        origin.mkdir()
        g(origin, "init", "-q")
        commit(origin, {"index.html": "v1"})
        g(self.tmp, "clone", "-q", str(origin), str(self.db / "site"))
        self.port = free_port()
        registry = {"site": {"name": "site", "kind": "static", "port": free_port()}}
        self.sup = oracle.Supervisor(self.db, registry, cfg, host="0.0.0.0", dashboard_port=self.port, quiet=True)
        self.httpd = oracle.make_dashboard(self.sup, self.port, bind="127.0.0.1")
        patch = mock.patch.object(oracle.DashboardHandler, "_client_ip", lambda handler: "192.168.1.50")
        patch.start()
        self.addCleanup(patch.stop)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body=None, cookie=None):
        headers = {"X-Oracle": "1", "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body or {}).encode(),
                      headers=headers, method="POST")
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, resp.headers
        except Exception as e:
            return getattr(e, "code", None), getattr(e, "headers", {})

    def test_other_devices_need_the_code_to_update(self):
        self.assertTrue(self.sup.lan)
        self.assertEqual(self.post("/api/update-all")[0], 401)
        self.assertEqual(self.post("/api/atlas/site/update")[0], 401)
        status, headers = self.post("/api/login", {"code": "k7mp-q2xa"})
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.post("/api/atlas/site/update", cookie=cookie)[0], 200)
        self.assertTrue(wait_for(lambda: self.sup.runner("site").note.startswith("already up to date")))
        self.assertEqual(self.post("/api/update-all", cookie=cookie)[0], 200)

    def test_apps_listen_on_the_network_in_lan_mode(self):
        cmd, env = self.sup.runner("site").command()
        self.assertEqual((cmd[cmd.index("--host") + 1], env["HOST"]), ("0.0.0.0", "0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
