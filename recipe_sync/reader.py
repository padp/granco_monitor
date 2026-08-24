"""Chunked batched read of the whole RECIPE_STORED[0..499] library."""
from . import config


def _tags_for_index(i: int) -> list:
    base = f"RECIPE_STORED[{i}]"
    return [f"{base}.{sub_tag}" for sub_tag in config.RECIPE_SUB_TAGS]


def read_all_recipes(plc_client) -> list:
    """Reads every recipe slot, chunked config.RECIPE_CHUNK_SIZE recipes
    at a time (see config.py for why - not one huge batched call).
    Returns one dict per slot: {index, populated, name, ...all fields},
    "populated" meaning a non-blank name (the vast majority of slots are
    unused). A tag that failed to read comes back as None (see
    PlcClient.read_all) rather than dropping the whole slot."""
    recipes = []
    for chunk_start in range(0, config.MAX_RECIPE_INDEX, config.RECIPE_CHUNK_SIZE):
        chunk_indices = range(chunk_start, min(chunk_start + config.RECIPE_CHUNK_SIZE, config.MAX_RECIPE_INDEX))
        tags = [tag for i in chunk_indices for tag in _tags_for_index(i)]
        values = plc_client.read_all(tags)

        for i in chunk_indices:
            base = f"RECIPE_STORED[{i}]"
            fields = {
                field_name: values.get(f"{base}.{sub_tag}")
                for sub_tag, field_name in config.RECIPE_SUB_TAGS.items()
            }
            name = fields.get("name")
            recipes.append({
                "index": i,
                "populated": bool(name and str(name).strip()),
                **fields,
            })

    return recipes
