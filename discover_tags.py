"""
Read-only tag discovery for the Granco saw PLC.

Run this on a machine that can reach the saw's network, then send back
discovered_tags.csv and the console output showing which SAWBLADE members
actually exist. Used to confirm the tag list from the 2023 collector is
still current and to spot anything new (fault/alarm bits, queue state,
etc.) worth adding to the live collector.
"""
from pylogix import PLC

PLC_IP = "10.4.20.21"

# SAWBLADE is read today only as SAWBLADE.ActualPosition, but it looks like
# a motion axis instance (AXIS_CIP_DRIVE or similar), which usually exposes
# more members than that. Probing common ones read-only to see what's there.
SAWBLADE_MEMBERS_TO_PROBE = [
    "ActualPosition",
    "ActualVelocity",
    "CommandPosition",
    "CommandVelocity",
    "AxisFaultStatus",
    "AxisStatus",
    "ServoActionStatus",
]


def main():
    with PLC() as comm:
        comm.IPAddress = PLC_IP

        print(f"Connecting to {PLC_IP} ...")
        result = comm.GetTagList()
        if result.Status != "Success":
            print(f"GetTagList failed: {result.Status}")
            return

        with open("discovered_tags.csv", "w", newline="", encoding="utf-8") as f:
            f.write("tag_name,data_type\n")
            for tag in result.Value:
                f.write(f"{tag.TagName},{tag.DataType}\n")
        print(f"Wrote {len(result.Value)} tags to discovered_tags.csv")

        print("\nProbing SAWBLADE.<member> candidates:")
        for member in SAWBLADE_MEMBERS_TO_PROBE:
            r = comm.Read(f"SAWBLADE.{member}")
            status = "OK" if r.Status == "Success" else r.Status
            print(f"  SAWBLADE.{member}: {status} -> {r.Value}")


if __name__ == "__main__":
    main()
