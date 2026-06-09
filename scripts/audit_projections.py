"""Compare REST-API projection against CouchDB full doc for each endpoint.

For every endpoint that returns a list of documents, fetch one item via
the REST API and the same item via /api/doc?id=, then report which keys
are present only in the full doc (i.e. stripped by the REST projection)
or have a different value/shape.

Goal: identify endpoints where the REST projection hides fields useful
to a consuming LLM, like the habits.get_habits / .title situation.
"""
from amazing_marvin_mcp.api import create_api_client


def diff(rest: dict, full: dict, *, ignore=("_rev", "fieldUpdates")) -> dict:
    rk = set(rest.keys())
    fk = set(full.keys())
    only_full = sorted(k for k in fk - rk if k not in ignore)
    only_rest = sorted(k for k in rk - fk if k not in ignore)
    different = {}
    for k in rk & fk:
        if k in ignore:
            continue
        rv, fv = rest.get(k), full.get(k)
        if type(rv) is not type(fv) or rv != fv:
            different[k] = {"rest": rv, "full": fv}
    return {"only_in_full": only_full, "only_in_rest": only_rest, "different": different}


def show(label, items, get_id=lambda x: x.get("_id") or x.get("habitId")):
    c = create_api_client()
    if not items:
        print(f"\n=== {label}: no items returned ===")
        return
    sample = items[0]
    iid = get_id(sample)
    if not iid:
        print(f"\n=== {label}: no _id on sample (keys: {sorted(sample.keys())}) ===")
        return
    try:
        full = c.get_document(iid)
    except Exception as e:
        print(f"\n=== {label}: get_document({iid!r}) failed: {e} ===")
        return
    d = diff(sample, full)
    print(f"\n=== {label} (sample _id={iid}) ===")
    print(f"  REST keys      : {sorted(sample.keys())}")
    print(f"  Only in full   : {d['only_in_full']}")
    if d["only_in_rest"]:
        print(f"  Only in REST   : {d['only_in_rest']}")
    if d["different"]:
        # Just key names + simple kinds
        for k, v in d["different"].items():
            rt = type(v['rest']).__name__
            ft = type(v['full']).__name__
            marker = "  TYPE-DIFF" if rt != ft else "  VAL-DIFF"
            print(f"  {marker} {k!r}: rest={rt} {v['rest']!r} | full={ft} {v['full']!r}")


def main():
    c = create_api_client()
    print(f"has_couchdb={c.has_couchdb}")

    show("get_categories", c.get_categories())
    show("get_projects",   c.get_projects())
    show("get_labels",     c.get_labels())
    show("get_goals",      c.get_goals())
    show("get_tasks",      c.get_tasks())
    show("get_due_items",  c.get_due_items())
    show("get_today_time_blocks",
         c._make_request("get", "/todayTimeBlocks") if hasattr(c, "_make_request") else [])
    show("get_habits",     c.get_habits(), get_id=lambda x: x.get("habitId"))


if __name__ == "__main__":
    main()
