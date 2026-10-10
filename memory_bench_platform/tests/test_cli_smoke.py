from memory_bench_platform.cli import build_parser


def test_build_parser_exposes_expected_subcommands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"list-skills", "plan-run", "run", "validate", "analyze-run", "score-run", "run-smoke"} <= set(choices)
    assert {"deploy", "undeploy"} <= set(choices)
    assert build_parser().parse_args(["deploy", "--config", "suite.yaml", "--agent-config", 'c:level="debug"']).agent_config == ['c:level="debug"']
    assert build_parser().parse_args(["deploy", "status", "--deployment", "suite"]).deploy_action == "status"
    assert build_parser().parse_args(["run", "--deployment", "suite"]).deployment == "suite"
