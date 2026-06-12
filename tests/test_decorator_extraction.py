"""v0.13 P1: TS decorator + Java annotation extraction.

Foundation for Angular / Spring Boot framework detection: the
`decorators` column was Python-only; `_ts_node_decorators` fills it for
TS/JS/TSX (`decorator` nodes, including on a wrapping export_statement)
and Java (`annotation` / `marker_annotation` inside `modifiers`).
"""

from __future__ import annotations

from livespec_mcp.domain.extractors import extract


def _by_name(result):
    out = {}
    for s in result.symbols:
        out.setdefault(s.name, s)
    return out


def test_ts_exported_class_decorator(tmp_path):
    src = (
        "import { Component, HostListener } from '@angular/core';\n"
        "\n"
        "@Component({\n"
        "  selector: 'app-dash',\n"
        "  templateUrl: './dash.html',\n"
        "})\n"
        "export class DashComponent {\n"
        "  ngOnInit(): void {}\n"
        "\n"
        "  @HostListener('window:resize')\n"
        "  onResize(): void {}\n"
        "}\n"
    )
    p = tmp_path / "dash.component.ts"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["DashComponent"].decorators == ["Component"]
    assert syms["onResize"].decorators == ["HostListener"]
    assert syms["ngOnInit"].decorators == []


def test_ts_unexported_injectable(tmp_path):
    src = (
        "@Injectable({ providedIn: 'root' })\n"
        "class AuthService {\n"
        "  login(): void {}\n"
        "}\n"
    )
    p = tmp_path / "auth.service.ts"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["AuthService"].decorators == ["Injectable"]


def test_ts_member_expression_decorator(tmp_path):
    src = (
        "const registry = { register: () => (t: any) => t };\n"
        "\n"
        "@registry.register()\n"
        "export class Plugin {}\n"
    )
    p = tmp_path / "plugin.ts"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["Plugin"].decorators == ["registry.register"]


def test_java_spring_annotations(tmp_path):
    src = (
        "package com.example.api;\n"
        "\n"
        "import org.springframework.web.bind.annotation.*;\n"
        "\n"
        "@RestController\n"
        "@RequestMapping(\"/api/users\")\n"
        "public class UserController {\n"
        "\n"
        "    @GetMapping\n"
        "    public String list() {\n"
        "        return \"[]\";\n"
        "    }\n"
        "\n"
        "    @PostMapping(\"/create\")\n"
        "    public String create() {\n"
        "        return \"ok\";\n"
        "    }\n"
        "\n"
        "    @Override\n"
        "    public String toString() {\n"
        "        return \"UserController\";\n"
        "    }\n"
        "}\n"
    )
    p = tmp_path / "UserController.java"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["UserController"].decorators == ["RestController", "RequestMapping"]
    assert syms["list"].decorators == ["GetMapping"]
    assert syms["create"].decorators == ["PostMapping"]
    assert syms["toString"].decorators == ["Override"]


def test_java_dotted_annotation(tmp_path):
    src = (
        "@org.springframework.stereotype.Service\n"
        "public class BillingService {\n"
        "    public void charge() {}\n"
        "}\n"
    )
    p = tmp_path / "BillingService.java"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["BillingService"].decorators == ["org.springframework.stereotype.Service"]


def test_python_decorators_unchanged(tmp_path):
    src = "@app.get('/x')\ndef handler():\n    return 1\n"
    p = tmp_path / "api.py"
    p.write_text(src)
    _, result = extract(p, src, tmp_path)
    syms = _by_name(result)
    assert syms["handler"].decorators == ["app.get"]
