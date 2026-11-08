"""制作指令的可选材料约束；只改配方副本，不修改公共配置。"""
from copy import deepcopy


def split_material_options(tokens):
    positional, selections = [], {}
    for token in tokens:
        if token == "-":
            continue
        token = token.replace("＝", "=")
        if "=" not in token:
            positional.append(token)
            continue
        key, name = token.split("=", 1)
        if not key or not name or "=" in name:
            raise ValueError("材料参数格式：圣品灵植=幽冥草")
        if key in selections:
            raise ValueError(f"材料类别【{key}】重复指定")
        selections[key] = name
    return positional, selections


def select_recipe_materials(recipe, selections, material_info):
    if not selections:
        return recipe
    result = deepcopy(recipe)
    specs = result.get("materials", [])
    if isinstance(specs, dict):
        specs = [{"name": k, "count": v} for k, v in specs.items()]
        result["materials"] = specs
    for key, name in selections.items():
        matches = [s for s in specs if not s.get("name") and s.get("category")
                   and key == f"{s.get('tier', '')}{s['category']}"]
        if not matches:
            raise ValueError(f"配方不需要按类别选择【{key}】材料")
        info = material_info(name) or {}
        for spec in matches:
            if info.get("category") != spec["category"] or (
                spec.get("tier") and info.get("tier") != spec["tier"]
            ):
                raise ValueError(f"【{name}】不符合【{key}】材料要求")
            spec["name"] = name
    return result
