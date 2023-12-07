from pants.backend.adhoc.code_quality_tool import CodeQualityToolRuleBuilder


def rules(backend_package_alias: str, goal: str, target: str, name: str):
    cfg = CodeQualityToolRuleBuilder(
        goal=goal,
        target=target,
        name=name,
        scope=backend_package_alias,
    )
    return cfg.rules()