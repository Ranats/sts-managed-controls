using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Diagnostics.Runtime;

internal static class Program
{
    private const uint ProcessVmOperation = 0x0008;
    private const uint ProcessVmWrite = 0x0020;
    private const uint ProcessQueryInformation = 0x0400;

    private static readonly string[] RootTypes =
    {
        "MegaCrit.Sts2.Core.Runs.RunManager",
        "MegaCrit.Sts2.Core.Combat.CombatManager",
        "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold",
        "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp",
        "MegaCrit.Sts2.Core.Nodes.Combat.NEnergyCounter",
    };

    private static readonly string[] HeapTypes =
    {
        "MegaCrit.Sts2.Core.Runs.RunManager",
        "MegaCrit.Sts2.Core.Runs.RunState",
        "MegaCrit.Sts2.Core.Combat.CombatManager",
        "MegaCrit.Sts2.Core.Combat.CombatState",
        "MegaCrit.Sts2.Core.Entities.Players.Player",
        "MegaCrit.Sts2.Core.Entities.Players.PlayerCombatState",
        "MegaCrit.Sts2.Core.Entities.Creatures.Creature",
        "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold",
        "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp",
        "MegaCrit.Sts2.Core.Nodes.Combat.NEnergyCounter",
        "CreatureState",
        "PlayerState",
        "NetFullCombatState",
    };

    public static int Main(string[] args)
    {
        bool setPlayerGold = args.Length > 0 && string.Equals(args[0], "--set-player-gold", StringComparison.Ordinal);
        bool setPlayerEnergy = args.Length > 0 && string.Equals(args[0], "--set-player-energy", StringComparison.Ordinal);
        bool aliasPowers = args.Length > 0 && string.Equals(args[0], "--alias-powers", StringComparison.Ordinal);
        bool setPlayerBlock = args.Length > 0 && string.Equals(args[0], "--set-player-block", StringComparison.Ordinal);
        bool setPowerAmount = args.Length > 0 && string.Equals(args[0], "--set-power-amount", StringComparison.Ordinal);
        bool summaryJson = args.Length > 0 && string.Equals(args[0], "--summary-json", StringComparison.Ordinal);
        int pidIndex = summaryJson || setPlayerBlock || setPowerAmount || aliasPowers || setPlayerEnergy || setPlayerGold ? 1 : 0;
        if (args.Length <= pidIndex || !int.TryParse(args[pidIndex], NumberStyles.None, CultureInfo.InvariantCulture, out var pid))
        {
            Console.Error.WriteLine("usage: clrmd_probe [--summary-json] <pid> | [--set-player-block] <pid> <value> | [--set-player-gold] <pid> <value> | [--set-player-energy] <pid> <currentValue> [maxValue] | [--set-power-amount] <pid> <player|enemy> <powerType> <value> | [--alias-powers] <pid> <sourceTarget> <destTarget>");
            return 1;
        }

        int requestedBlock = int.MinValue;
        if (setPlayerBlock && (args.Length <= pidIndex + 1 || !int.TryParse(args[pidIndex + 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedBlock)))
        {
            Console.Error.WriteLine("usage: clrmd_probe --set-player-block <pid> <value>");
            return 1;
        }

        int requestedGold = int.MinValue;
        if (setPlayerGold && (args.Length <= pidIndex + 1 || !int.TryParse(args[pidIndex + 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedGold)))
        {
            Console.Error.WriteLine("usage: clrmd_probe --set-player-gold <pid> <value>");
            return 1;
        }

        int requestedEnergy = int.MinValue;
        int requestedMaxEnergy = int.MinValue;
        bool writeMaxEnergy = false;
        if (setPlayerEnergy)
        {
            if (args.Length <= pidIndex + 1 || !int.TryParse(args[pidIndex + 1], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedEnergy))
            {
                Console.Error.WriteLine("usage: clrmd_probe --set-player-energy <pid> <currentValue> [maxValue]");
                return 1;
            }

            if (args.Length > pidIndex + 2)
            {
                if (!int.TryParse(args[pidIndex + 2], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedMaxEnergy))
                {
                    Console.Error.WriteLine("usage: clrmd_probe --set-player-energy <pid> <currentValue> [maxValue]");
                    return 1;
                }

                writeMaxEnergy = true;
            }
        }

        string powerTarget = string.Empty;
        string powerType = string.Empty;
        int requestedPowerAmount = int.MinValue;
        if (setPowerAmount)
        {
            if (args.Length <= pidIndex + 3)
            {
                Console.Error.WriteLine("usage: clrmd_probe --set-power-amount <pid> <player|enemy> <powerType> <value>");
                return 1;
            }

            powerTarget = args[pidIndex + 1];
            powerType = args[pidIndex + 2];
            if (!int.TryParse(args[pidIndex + 3], NumberStyles.Integer, CultureInfo.InvariantCulture, out requestedPowerAmount))
            {
                Console.Error.WriteLine("usage: clrmd_probe --set-power-amount <pid> <player|enemy> <powerType> <value>");
                return 1;
            }
        }

        string aliasSource = string.Empty;
        string aliasDest = string.Empty;
        if (aliasPowers)
        {
            if (args.Length <= pidIndex + 2)
            {
                Console.Error.WriteLine("usage: clrmd_probe --alias-powers <pid> <sourceTarget> <destTarget>");
                return 1;
            }

            aliasSource = args[pidIndex + 1];
            aliasDest = args[pidIndex + 2];
        }

        try
        {
            using (DataTarget target = DataTarget.CreateSnapshotAndAttach(pid))
            {
                ClrInfo clrInfo = target.ClrVersions.Single();
                using (ClrRuntime runtime = clrInfo.CreateRuntime())
                {
                    Dictionary<string, List<ClrObject>> matches = FindHeapObjects(runtime, new HashSet<string>(HeapTypes, StringComparer.Ordinal));
                    if (setPlayerBlock)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(SetPlayerBlock(pid, clrInfo.Version.ToString(), runtime, matches, requestedBlock)));
                        return 0;
                    }

                    if (setPlayerGold)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(SetPlayerGold(pid, clrInfo.Version.ToString(), runtime, matches, requestedGold)));
                        return 0;
                    }

                    if (setPlayerEnergy)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(SetPlayerEnergy(pid, clrInfo.Version.ToString(), runtime, matches, requestedEnergy, writeMaxEnergy, requestedMaxEnergy)));
                        return 0;
                    }

                    if (aliasPowers)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(AliasPowers(pid, clrInfo.Version.ToString(), runtime, matches, aliasSource, aliasDest)));
                        return 0;
                    }

                    if (setPowerAmount)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(SetPowerAmount(pid, clrInfo.Version.ToString(), runtime, matches, powerTarget, powerType, requestedPowerAmount)));
                        return 0;
                    }

                    if (summaryJson)
                    {
                        Console.WriteLine(JsonSerializer.Serialize(BuildSummaryPayload(pid, clrInfo.Version.ToString(), runtime, matches)));
                        return 0;
                    }

                    Console.WriteLine("runtime {0}", clrInfo.Version);
                    Console.WriteLine("heap.canWalk {0}", runtime.Heap.CanWalkHeap);
                    Console.WriteLine();

                    foreach (string typeName in RootTypes)
                    {
                        ClrType type = FindType(runtime, typeName);
                        if (type == null)
                        {
                            Console.WriteLine("root.type.missing {0}", typeName);
                            continue;
                        }

                        DumpStaticType(type, runtime);
                        Console.WriteLine();
                    }

                    DumpSummary(matches);

                    foreach (string typeName in HeapTypes)
                    {
                        List<ClrObject> objects;
                        if (!matches.TryGetValue(typeName, out objects) || objects.Count == 0)
                        {
                            Console.WriteLine("heap.type.empty {0}", typeName);
                            Console.WriteLine();
                            continue;
                        }

                        Console.WriteLine("heap.type {0} count={1}", typeName, objects.Count);
                        foreach (ClrObject obj in objects.Where(ShouldDumpObject))
                        {
                            DumpObject(obj, depth: 1, seen: new HashSet<ulong>());
                        }

                        Console.WriteLine();
                    }

                    DumpRoots(runtime, matches);
                    DumpBackrefs(runtime, matches);
                }
            }

            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.GetType().FullName);
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine(ex.ToString());
            return 2;
        }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteProcessMemory(
        IntPtr processHandle,
        UIntPtr baseAddress,
        byte[] buffer,
        UIntPtr size,
        out UIntPtr bytesWritten);

    private static ClrType FindType(ClrRuntime runtime, string fullName)
    {
        foreach (ClrModule module in runtime.EnumerateModules())
        {
            ClrType type = module.GetTypeByName(fullName);
            if (type != null)
            {
                return type;
            }
        }

        return null;
    }

    private static void DumpStaticType(ClrType type, ClrRuntime runtime)
    {
        ClrAppDomain domain = type.Module != null && type.Module.AppDomain != null
            ? type.Module.AppDomain
            : runtime.AppDomains.FirstOrDefault() ?? runtime.SystemDomain ?? runtime.SharedDomain;

        Console.WriteLine("root.type {0}", type.Name);
        if (domain != null)
        {
            Console.WriteLine("  domain {0}", domain.Name);
        }

        foreach (ClrStaticField field in type.StaticFields)
        {
            Console.Write("  static {0} @+0x{1:x} => ", field.Name, field.Offset);
            Console.WriteLine(DescribeStaticField(field, domain));

            ClrObject child = ReadStaticObject(field, domain);
            if (!child.IsNull && child.Type != null && child.Type.Name != null && child.Type.Name.StartsWith("MegaCrit.", StringComparison.Ordinal))
            {
                DumpObject(child, depth: 1, seen: new HashSet<ulong>());
            }
        }
    }

    private static Dictionary<string, List<ClrObject>> FindHeapObjects(ClrRuntime runtime, HashSet<string> typeNames)
    {
        Dictionary<string, List<ClrObject>> results = new Dictionary<string, List<ClrObject>>(StringComparer.Ordinal);

        foreach (ClrObject obj in runtime.Heap.EnumerateObjects())
        {
            ClrType type = obj.Type;
            string name = type != null ? type.Name : null;
            if (name == null || !typeNames.Contains(name))
            {
                continue;
            }

            List<ClrObject> list;
            if (!results.TryGetValue(name, out list))
            {
                list = new List<ClrObject>();
                results[name] = list;
            }

            list.Add(obj);
        }

        return results;
    }

    private static void DumpObject(ClrObject obj, int depth, HashSet<ulong> seen)
    {
        if (obj.IsNull || obj.Type == null)
        {
            return;
        }

        if (!seen.Add(obj.Address))
        {
            Console.WriteLine("  object 0x{0:x} {1} (seen)", obj.Address, obj.Type.Name);
            return;
        }

        Console.WriteLine("  object 0x{0:x} {1}", obj.Address, obj.Type.Name);
        foreach (ClrInstanceField field in obj.Type.Fields)
        {
            Console.Write("    field {0} @+0x{1:x} => ", field.Name, field.Offset);
            Console.WriteLine(DescribeInstanceField(obj, field));
        }

        if (depth <= 0)
        {
            return;
        }

        foreach (ClrInstanceField field in obj.Type.Fields)
        {
            ClrObject child = ReadInstanceObject(obj, field);
            if (child.IsNull || child.Type == null || child.Type.Name == null)
            {
                continue;
            }

            if (!child.Type.Name.StartsWith("MegaCrit.", StringComparison.Ordinal))
            {
                continue;
            }

            DumpObject(child, depth - 1, seen);
        }
    }

    private static bool ShouldDumpObject(ClrObject obj)
    {
        if (obj.IsNull || obj.Type == null || obj.Type.Name == null)
        {
            return false;
        }

        string typeName = obj.Type.Name;
        if (typeName == "MegaCrit.Sts2.Core.Entities.Players.Player" ||
            typeName == "MegaCrit.Sts2.Core.Entities.Players.PlayerCombatState" ||
            typeName == "MegaCrit.Sts2.Core.Runs.RunState" ||
            typeName == "MegaCrit.Sts2.Core.Runs.RunManager" ||
            typeName == "MegaCrit.Sts2.Core.Combat.CombatManager" ||
            typeName == "MegaCrit.Sts2.Core.Combat.CombatState" ||
            typeName == "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold" ||
            typeName == "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp" ||
            typeName == "MegaCrit.Sts2.Core.Nodes.Combat.NEnergyCounter")
        {
            return true;
        }

        if (typeName == "MegaCrit.Sts2.Core.Entities.Creatures.Creature")
        {
            int currentHp = ReadIntField(obj, "_currentHp");
            int maxHp = ReadIntField(obj, "_maxHp");
            return currentHp == 29 || currentHp == 53 || currentHp == 61 || maxHp == 61 || maxHp == 80;
        }

        return false;
    }

    private static void DumpSummary(Dictionary<string, List<ClrObject>> matches)
    {
        Dictionary<string, object> summary = BuildSummary(pid: 0, runtimeVersion: "", matches: matches);
        Console.WriteLine(
            "summary floor={0} ascension={1} gold={2} hp={3}/{4} energy={5}/{6}",
            summary["floor"],
            summary["ascension"],
            summary["gold"],
            summary["hp"],
            summary["max_hp"],
            summary["energy"],
            summary["max_energy"]);
        Console.WriteLine();
    }

    private static Dictionary<string, object> BuildSummaryPayload(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches)
    {
        Dictionary<string, object> summary = BuildSummary(pid, runtimeVersion, matches);
        ClrObject goldNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold");
        ClrObject hpNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp");
        ClrObject combatManager = FirstObject(matches, "MegaCrit.Sts2.Core.Combat.CombatManager");
        ClrObject player = ReadNamedObjectField(goldNode, "_player");
        if (player.IsNull)
        {
            player = ReadNamedObjectField(hpNode, "_player");
        }

        ClrObject creature = ReadNamedObjectField(player, "<Creature>k__BackingField");
        ClrObject combatState = ReadNamedObjectField(creature, "<CombatState>k__BackingField");
        if (combatState.IsNull)
        {
            combatState = ReadNamedObjectField(combatManager, "_state");
        }

        summary["player_powers"] = FindPowersForTarget(runtime, creature);
        summary["enemies"] = FindLiveEnemies(matches, combatState)
            .Select(
                enemy => (object)new Dictionary<string, object>
                {
                    ["address"] = FormatAddress(enemy),
                    ["current_hp"] = ReadIntField(enemy, "_currentHp"),
                    ["max_hp"] = ReadIntField(enemy, "_maxHp"),
                    ["block"] = ReadIntField(enemy, "_block"),
                    ["powers"] = FindPowersForTarget(runtime, enemy),
                })
            .ToList();
        return summary;
    }

    private static Dictionary<string, object> BuildSummary(int pid, string runtimeVersion, Dictionary<string, List<ClrObject>> matches)
    {
        ClrObject goldNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold");
        ClrObject hpNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp");
        ClrObject runManager = FirstObject(matches, "MegaCrit.Sts2.Core.Runs.RunManager");
        ClrObject combatManager = FirstObject(matches, "MegaCrit.Sts2.Core.Combat.CombatManager");

        ClrObject player = ReadNamedObjectField(goldNode, "_player");
        if (player.IsNull)
        {
            player = ReadNamedObjectField(hpNode, "_player");
        }

        ClrObject creature = ReadNamedObjectField(player, "<Creature>k__BackingField");
        ClrObject playerCombatState = ReadNamedObjectField(player, "<PlayerCombatState>k__BackingField");
        ClrObject runState = ReadNamedObjectField(player, "_runState");
        ClrObject powerList = ReadNamedObjectField(creature, "_powers");

        int gold = ReadIntField(goldNode, "_currentGold");
        if (gold == int.MinValue)
        {
            gold = ReadIntField(player, "_gold");
        }

        ClrObject combatState = ReadNamedObjectField(creature, "<CombatState>k__BackingField");
        if (combatState.IsNull)
        {
            combatState = ReadNamedObjectField(combatManager, "_state");
        }

        return new Dictionary<string, object>
        {
            ["pid"] = pid,
            ["runtime_version"] = runtimeVersion,
            ["floor"] = ReadIntField(runState, "<ActFloor>k__BackingField"),
            ["ascension"] = ReadIntField(runState, "<AscensionLevel>k__BackingField"),
            ["gold"] = gold,
            ["hp"] = ReadIntField(creature, "_currentHp"),
            ["max_hp"] = ReadIntField(creature, "_maxHp"),
            ["block"] = ReadIntField(creature, "_block"),
            ["energy"] = ReadIntField(playerCombatState, "_energy"),
            ["max_energy"] = ReadIntField(player, "<MaxEnergy>k__BackingField"),
            ["objects"] = new Dictionary<string, object>
            {
                ["run_manager"] = FormatAddress(runManager),
                ["run_state"] = FormatAddress(runState),
                ["combat_manager"] = FormatAddress(combatManager),
                ["combat_state"] = FormatAddress(combatState),
                ["player"] = FormatAddress(player),
                ["player_creature"] = FormatAddress(creature),
                ["player_combat_state"] = FormatAddress(playerCombatState),
                ["player_power_list"] = FormatAddress(powerList),
                ["topbar_gold"] = FormatAddress(goldNode),
                ["topbar_hp"] = FormatAddress(hpNode),
            },
            ["sources"] = new Dictionary<string, object>
            {
                ["gold"] = "NTopBarGold._currentGold",
                ["hp"] = "Player.<Creature>._currentHp/_maxHp",
                ["block"] = "Player.<Creature>._block",
                ["energy"] = "Player.<PlayerCombatState>._energy",
                ["floor"] = "RunState.<ActFloor>k__BackingField",
                ["ascension"] = "RunState.<AscensionLevel>k__BackingField",
            },
        };
    }

    private static string DescribeStaticField(ClrStaticField field, ClrAppDomain domain)
    {
        if (field == null)
        {
            return "<null field>";
        }

        if (domain == null)
        {
            return "<no domain>";
        }

        ClrType type = field.Type;
        if (type == null)
        {
            return "<no type>";
        }

        try
        {
            switch (type.ElementType)
            {
                case ClrElementType.Boolean:
                    return field.Read<bool>(domain).ToString();
                case ClrElementType.Int32:
                    return field.Read<int>(domain).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt32:
                    return field.Read<uint>(domain).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Int64:
                    return field.Read<long>(domain).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt64:
                    return field.Read<ulong>(domain).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.String:
                    return field.ReadString(domain);
                default:
                    ClrObject obj = field.ReadObject(domain);
                    if (obj.IsNull || obj.Type == null)
                    {
                        return "null";
                    }

                    return string.Format(CultureInfo.InvariantCulture, "0x{0:x} {1}", obj.Address, obj.Type.Name);
            }
        }
        catch (Exception ex)
        {
            return string.Format(CultureInfo.InvariantCulture, "<error {0}: {1}>", ex.GetType().Name, ex.Message);
        }
    }

    private static string DescribeInstanceField(ClrObject obj, ClrInstanceField field)
    {
        if (field == null)
        {
            return "<null field>";
        }

        ClrType type = field.Type;
        if (type == null)
        {
            return "<no type>";
        }

        try
        {
            switch (type.ElementType)
            {
                case ClrElementType.Boolean:
                    return field.Read<bool>(obj.Address, false).ToString();
                case ClrElementType.Int8:
                    return field.Read<sbyte>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt8:
                    return field.Read<byte>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Int16:
                    return field.Read<short>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt16:
                    return field.Read<ushort>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Int32:
                    return field.Read<int>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt32:
                    return field.Read<uint>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Int64:
                    return field.Read<long>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.UInt64:
                    return field.Read<ulong>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Float:
                    return field.Read<float>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.Double:
                    return field.Read<double>(obj.Address, false).ToString(CultureInfo.InvariantCulture);
                case ClrElementType.String:
                    return field.ReadString(obj.Address, false);
                default:
                    ClrObject child = field.ReadObject(obj.Address, false);
                    if (child.IsNull || child.Type == null)
                    {
                        return "null";
                    }

                    if (child.Type.IsArray)
                    {
                        return string.Format(
                            CultureInfo.InvariantCulture,
                            "0x{0:x} {1} len={2}",
                            child.Address,
                            child.Type.Name,
                            child.AsArray().Length);
                    }

                    return string.Format(CultureInfo.InvariantCulture, "0x{0:x} {1}", child.Address, child.Type.Name);
            }
        }
        catch (Exception ex)
        {
            return string.Format(CultureInfo.InvariantCulture, "<error {0}: {1}>", ex.GetType().Name, ex.Message);
        }
    }

    private static ClrObject ReadStaticObject(ClrStaticField field, ClrAppDomain domain)
    {
        if (field == null || domain == null)
        {
            return default(ClrObject);
        }

        try
        {
            return field.ReadObject(domain);
        }
        catch
        {
            return default(ClrObject);
        }
    }

    private static ClrObject ReadInstanceObject(ClrObject obj, ClrInstanceField field)
    {
        if (obj.IsNull || field == null)
        {
            return default(ClrObject);
        }

        try
        {
            return field.ReadObject(obj.Address, false);
        }
        catch
        {
            return default(ClrObject);
        }
    }

    private static int ReadIntField(ClrObject obj, string fieldName)
    {
        try
        {
            return obj.ReadField<int>(fieldName);
        }
        catch
        {
            return int.MinValue;
        }
    }

    private static ClrObject ReadNamedObjectField(ClrObject obj, string fieldName)
    {
        try
        {
            return obj.ReadObjectField(fieldName);
        }
        catch
        {
            return default(ClrObject);
        }
    }

    private static string FormatAddress(ClrObject obj)
    {
        return obj.IsNull ? "" : string.Format(CultureInfo.InvariantCulture, "0x{0:x}", obj.Address);
    }

    private static Dictionary<string, object> SetPlayerBlock(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches,
        int requestedBlock)
    {
        ClrObject creature = ResolvePlayerCreature(matches);
        if (creature.IsNull || creature.Type == null)
        {
            throw new InvalidOperationException("player creature not found");
        }

        ClrInstanceField blockField = creature.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_block", StringComparison.Ordinal));
        if (blockField == null)
        {
            throw new InvalidOperationException("Creature._block field not found");
        }

        ulong fieldAddress = blockField.GetAddress(creature.Address, false);
        int previousBlock = ReadIntField(creature, "_block");
        WriteInt32(pid, fieldAddress, requestedBlock);

        Dictionary<string, object> summary = BuildSummaryPayload(pid, runtimeVersion, runtime, matches);
        summary["block"] = requestedBlock;
        summary["write"] = new Dictionary<string, object>
        {
            ["field"] = "player_block",
            ["address"] = string.Format(CultureInfo.InvariantCulture, "0x{0:x}", fieldAddress),
            ["previous"] = previousBlock,
            ["requested"] = requestedBlock,
        };
        return summary;
    }

    private static Dictionary<string, object> SetPlayerGold(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches,
        int requestedGold)
    {
        ClrObject goldNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold");
        ClrObject hpNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp");
        ClrObject player = ReadNamedObjectField(goldNode, "_player");
        if (player.IsNull)
        {
            player = ReadNamedObjectField(hpNode, "_player");
        }

        if (player.IsNull || player.Type == null)
        {
            throw new InvalidOperationException("player not found");
        }

        ClrInstanceField goldField = player.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_gold", StringComparison.Ordinal));
        if (goldField == null)
        {
            throw new InvalidOperationException("Player._gold field not found");
        }

        ulong goldAddress = goldField.GetAddress(player.Address, false);
        int previousGold = ReadIntField(player, "_gold");
        WriteInt32(pid, goldAddress, requestedGold);

        ulong uiAddress = 0;
        int previousUiGold = int.MinValue;
        bool wroteUiGold = false;
        if (!goldNode.IsNull && goldNode.Type != null)
        {
            ClrInstanceField uiField = goldNode.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_currentGold", StringComparison.Ordinal));
            if (uiField != null)
            {
                uiAddress = uiField.GetAddress(goldNode.Address, false);
                previousUiGold = ReadIntField(goldNode, "_currentGold");
                WriteInt32(pid, uiAddress, requestedGold);
                wroteUiGold = true;
            }
        }

        Dictionary<string, object> summary = BuildSummaryPayload(pid, runtimeVersion, runtime, matches);
        summary["gold"] = requestedGold;
        summary["write"] = new Dictionary<string, object>
        {
            ["field"] = "player_gold",
            ["gold_address"] = string.Format(CultureInfo.InvariantCulture, "0x{0:x}", goldAddress),
            ["previous_gold"] = previousGold,
            ["requested_gold"] = requestedGold,
            ["ui_address"] = uiAddress == 0 ? "" : string.Format(CultureInfo.InvariantCulture, "0x{0:x}", uiAddress),
            ["previous_ui_gold"] = previousUiGold == int.MinValue ? previousGold : previousUiGold,
            ["wrote_ui_gold"] = wroteUiGold,
        };
        return summary;
    }

    private static Dictionary<string, object> SetPlayerEnergy(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches,
        int requestedEnergy,
        bool writeMaxEnergy,
        int requestedMaxEnergy)
    {
        ClrObject goldNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold");
        ClrObject hpNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp");
        ClrObject player = ReadNamedObjectField(goldNode, "_player");
        if (player.IsNull)
        {
            player = ReadNamedObjectField(hpNode, "_player");
        }

        if (player.IsNull || player.Type == null)
        {
            throw new InvalidOperationException("player not found");
        }

        ClrObject playerCombatState = ReadNamedObjectField(player, "<PlayerCombatState>k__BackingField");
        if (playerCombatState.IsNull || playerCombatState.Type == null)
        {
            throw new InvalidOperationException("player combat state not found");
        }

        ClrInstanceField energyField = playerCombatState.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_energy", StringComparison.Ordinal));
        if (energyField == null)
        {
            throw new InvalidOperationException("PlayerCombatState._energy field not found");
        }

        ulong energyAddress = energyField.GetAddress(playerCombatState.Address, false);
        int previousEnergy = ReadIntField(playerCombatState, "_energy");
        WriteInt32(pid, energyAddress, requestedEnergy);

        int previousMaxEnergy = ReadIntField(player, "<MaxEnergy>k__BackingField");
        ulong maxEnergyAddress = 0;
        if (writeMaxEnergy)
        {
            ClrInstanceField maxEnergyField = player.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "<MaxEnergy>k__BackingField", StringComparison.Ordinal));
            if (maxEnergyField == null)
            {
                throw new InvalidOperationException("Player.<MaxEnergy>k__BackingField field not found");
            }

            maxEnergyAddress = maxEnergyField.GetAddress(player.Address, false);
            WriteInt32(pid, maxEnergyAddress, requestedMaxEnergy);
        }

        Dictionary<string, object> summary = BuildSummaryPayload(pid, runtimeVersion, runtime, matches);
        summary["energy"] = requestedEnergy;
        if (writeMaxEnergy)
        {
            summary["max_energy"] = requestedMaxEnergy;
        }

        summary["write"] = new Dictionary<string, object>
        {
            ["field"] = "player_energy",
            ["energy_address"] = string.Format(CultureInfo.InvariantCulture, "0x{0:x}", energyAddress),
            ["previous_energy"] = previousEnergy,
            ["requested_energy"] = requestedEnergy,
            ["max_energy_address"] = maxEnergyAddress == 0 ? "" : string.Format(CultureInfo.InvariantCulture, "0x{0:x}", maxEnergyAddress),
            ["previous_max_energy"] = previousMaxEnergy,
            ["requested_max_energy"] = writeMaxEnergy ? requestedMaxEnergy : previousMaxEnergy,
            ["wrote_max_energy"] = writeMaxEnergy,
        };
        return summary;
    }

    private static Dictionary<string, object> SetPowerAmount(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches,
        string targetKind,
        string powerType,
        int requestedAmount)
    {
        ClrObject targetCreature;
        ClrObject power = ResolvePower(matches, targetKind, powerType, out targetCreature);
        if (power.IsNull || power.Type == null)
        {
            throw new InvalidOperationException(
                string.Format(
                    CultureInfo.InvariantCulture,
                    "power not found: {0}/{1}; available=[{2}]",
                    targetKind,
                    powerType,
                    string.Join(", ", ListAvailablePowers(matches, targetKind))));
        }

        ClrInstanceField amountField = power.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_amount", StringComparison.Ordinal));
        if (amountField == null)
        {
            throw new InvalidOperationException("PowerModel._amount field not found");
        }

        ulong fieldAddress = amountField.GetAddress(power.Address, false);
        int previousAmount = ReadIntField(power, "_amount");
        WriteInt32(pid, fieldAddress, requestedAmount);

        Dictionary<string, object> summary = BuildSummaryPayload(pid, runtimeVersion, runtime, matches);
        PatchSummaryPowerAmount(summary, targetKind, power, requestedAmount);
        summary["write"] = new Dictionary<string, object>
        {
            ["field"] = "power_amount",
            ["target"] = targetKind,
            ["power_type"] = power.Type.Name,
            ["power_address"] = FormatAddress(power),
            ["target_creature"] = FormatAddress(targetCreature),
            ["address"] = string.Format(CultureInfo.InvariantCulture, "0x{0:x}", fieldAddress),
            ["previous"] = previousAmount,
            ["requested"] = requestedAmount,
        };
        return summary;
    }

    private static Dictionary<string, object> AliasPowers(
        int pid,
        string runtimeVersion,
        ClrRuntime runtime,
        Dictionary<string, List<ClrObject>> matches,
        string sourceTarget,
        string destTarget)
    {
        ClrObject sourceCreature = ResolveCreature(matches, sourceTarget);
        ClrObject destCreature = ResolveCreature(matches, destTarget);
        if (sourceCreature.IsNull || destCreature.IsNull)
        {
            throw new InvalidOperationException(
                string.Format(
                    CultureInfo.InvariantCulture,
                    "alias target not found: source={0} dest={1}",
                    sourceTarget,
                    destTarget));
        }

        ClrObject sourcePowers = ReadNamedObjectField(sourceCreature, "_powers");
        if (sourcePowers.IsNull)
        {
            throw new InvalidOperationException(string.Format(CultureInfo.InvariantCulture, "source target has no power list: {0}", sourceTarget));
        }

        ClrInstanceField powersField = destCreature.Type == null
            ? null
            : destCreature.Type.Fields.FirstOrDefault(field => string.Equals(field.Name, "_powers", StringComparison.Ordinal));
        if (powersField == null)
        {
            throw new InvalidOperationException("Creature._powers field not found on destination");
        }

        ulong fieldAddress = powersField.GetAddress(destCreature.Address, false);
        ClrObject previousPowers = ReadNamedObjectField(destCreature, "_powers");
        WriteUInt64(pid, fieldAddress, sourcePowers.Address);

        Dictionary<string, object> summary = BuildSummaryPayload(pid, runtimeVersion, runtime, matches);
        summary["write"] = new Dictionary<string, object>
        {
            ["field"] = "power_list_alias",
            ["source"] = sourceTarget,
            ["dest"] = destTarget,
            ["source_creature"] = FormatAddress(sourceCreature),
            ["dest_creature"] = FormatAddress(destCreature),
            ["previous"] = FormatAddress(previousPowers),
            ["requested"] = FormatAddress(sourcePowers),
            ["address"] = string.Format(CultureInfo.InvariantCulture, "0x{0:x}", fieldAddress),
        };
        return summary;
    }

    private static ClrObject ResolvePlayerCreature(Dictionary<string, List<ClrObject>> matches)
    {
        ClrObject goldNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold");
        ClrObject hpNode = FirstObject(matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarHp");
        ClrObject player = ReadNamedObjectField(goldNode, "_player");
        if (player.IsNull)
        {
            player = ReadNamedObjectField(hpNode, "_player");
        }

        return ReadNamedObjectField(player, "<Creature>k__BackingField");
    }

    private static ClrObject ResolvePower(
        Dictionary<string, List<ClrObject>> matches,
        string targetKind,
        string powerType,
        out ClrObject targetCreature)
    {
        targetCreature = default(ClrObject);
        if (string.Equals(targetKind, "player", StringComparison.OrdinalIgnoreCase))
        {
            targetCreature = ResolvePlayerCreature(matches);
            return FindPowerInCreature(targetCreature, powerType);
        }

        if (string.Equals(targetKind, "enemy", StringComparison.OrdinalIgnoreCase))
        {
            ClrObject combatManager = FirstObject(matches, "MegaCrit.Sts2.Core.Combat.CombatManager");
            ClrObject combatState = ReadNamedObjectField(combatManager, "_state");
            foreach (ClrObject enemy in FindLiveEnemies(matches, combatState))
            {
                ClrObject power = FindPowerInCreature(enemy, powerType);
                if (!power.IsNull)
                {
                    targetCreature = enemy;
                    return power;
                }
            }
        }

        return default(ClrObject);
    }

    private static ClrObject ResolveCreature(Dictionary<string, List<ClrObject>> matches, string targetKind)
    {
        if (string.Equals(targetKind, "player", StringComparison.OrdinalIgnoreCase))
        {
            return ResolvePlayerCreature(matches);
        }

        if (string.Equals(targetKind, "enemy", StringComparison.OrdinalIgnoreCase))
        {
            ClrObject combatManager = FirstObject(matches, "MegaCrit.Sts2.Core.Combat.CombatManager");
            ClrObject combatState = ReadNamedObjectField(combatManager, "_state");
            return FindLiveEnemies(matches, combatState).FirstOrDefault();
        }

        return default(ClrObject);
    }

    private static IEnumerable<string> ListAvailablePowers(Dictionary<string, List<ClrObject>> matches, string targetKind)
    {
        if (string.Equals(targetKind, "player", StringComparison.OrdinalIgnoreCase))
        {
            return FindPowersInCreatureList(ResolvePlayerCreature(matches))
                .OfType<Dictionary<string, object>>()
                .Select(payload => payload["type"] as string)
                .Where(name => !string.IsNullOrEmpty(name))
                .ToList();
        }

        if (string.Equals(targetKind, "enemy", StringComparison.OrdinalIgnoreCase))
        {
            ClrObject combatManager = FirstObject(matches, "MegaCrit.Sts2.Core.Combat.CombatManager");
            ClrObject combatState = ReadNamedObjectField(combatManager, "_state");
            return FindLiveEnemies(matches, combatState)
                .SelectMany(FindPowersInCreatureList)
                .OfType<Dictionary<string, object>>()
                .Select(payload => payload["type"] as string)
                .Where(name => !string.IsNullOrEmpty(name))
                .Distinct(StringComparer.Ordinal)
                .ToList();
        }

        return Array.Empty<string>();
    }

    private static ClrObject FindPowerInCreature(ClrObject creature, string powerType)
    {
        ClrObject powerList = ReadNamedObjectField(creature, "_powers");
        if (powerList.IsNull)
        {
            return default(ClrObject);
        }

        int size = ReadIntField(powerList, "_size");
        if (size <= 0)
        {
            return default(ClrObject);
        }

        ClrObject items = ReadNamedObjectField(powerList, "_items");
        if (items.IsNull || items.Type == null || !items.Type.IsArray)
        {
            return default(ClrObject);
        }

        Microsoft.Diagnostics.Runtime.ClrArray array = items.AsArray();
        int count = Math.Min(size, array.Length);
        for (int index = 0; index < count; index++)
        {
            ClrObject power = array.GetObjectValue(index);
            if (power.IsNull || power.Type == null || power.Type.Name == null)
            {
                continue;
            }

            if (power.Type.Name.EndsWith(powerType, StringComparison.Ordinal) ||
                power.Type.Name.EndsWith("." + powerType, StringComparison.Ordinal))
            {
                return power;
            }
        }

        return default(ClrObject);
    }

    private static void PatchSummaryPowerAmount(Dictionary<string, object> summary, string targetKind, ClrObject power, int requestedAmount)
    {
        Dictionary<string, object> powerPayload = BuildPowerPayload(power);
        powerPayload["amount"] = requestedAmount;

        if (string.Equals(targetKind, "player", StringComparison.OrdinalIgnoreCase))
        {
            List<object> playerPowers = summary["player_powers"] as List<object>;
            if (playerPowers == null)
            {
                return;
            }

            for (int i = 0; i < playerPowers.Count; i++)
            {
                Dictionary<string, object> existing = playerPowers[i] as Dictionary<string, object>;
                if (existing != null && string.Equals(existing["address"] as string, powerPayload["address"] as string, StringComparison.Ordinal))
                {
                    playerPowers[i] = powerPayload;
                    return;
                }
            }

            playerPowers.Add(powerPayload);
            return;
        }

        List<object> enemies = summary["enemies"] as List<object>;
        if (enemies == null)
        {
            return;
        }

        foreach (Dictionary<string, object> enemy in enemies.OfType<Dictionary<string, object>>())
        {
            List<object> powers = enemy["powers"] as List<object>;
            if (powers == null)
            {
                continue;
            }

            for (int i = 0; i < powers.Count; i++)
            {
                Dictionary<string, object> existing = powers[i] as Dictionary<string, object>;
                if (existing != null && string.Equals(existing["address"] as string, powerPayload["address"] as string, StringComparison.Ordinal))
                {
                    powers[i] = powerPayload;
                    return;
                }
            }
        }
    }

    private static void WriteInt32(int pid, ulong address, int value)
    {
        IntPtr processHandle = OpenProcess(ProcessVmOperation | ProcessVmWrite | ProcessQueryInformation, false, pid);
        if (processHandle == IntPtr.Zero)
        {
            throw new InvalidOperationException(string.Format(CultureInfo.InvariantCulture, "OpenProcess failed: {0}", Marshal.GetLastWin32Error()));
        }

        try
        {
            byte[] buffer = BitConverter.GetBytes(value);
            UIntPtr bytesWritten;
            bool ok = WriteProcessMemory(processHandle, new UIntPtr(address), buffer, new UIntPtr((uint)buffer.Length), out bytesWritten);
            if (!ok || bytesWritten.ToUInt64() != (ulong)buffer.Length)
            {
                throw new InvalidOperationException(
                    string.Format(
                        CultureInfo.InvariantCulture,
                        "WriteProcessMemory failed: ok={0} bytes={1} error={2}",
                        ok,
                        bytesWritten.ToUInt64(),
                        Marshal.GetLastWin32Error()));
            }
        }
        finally
        {
            CloseHandle(processHandle);
        }
    }

    private static void WriteUInt64(int pid, ulong address, ulong value)
    {
        IntPtr processHandle = OpenProcess(ProcessVmOperation | ProcessVmWrite | ProcessQueryInformation, false, pid);
        if (processHandle == IntPtr.Zero)
        {
            throw new InvalidOperationException(string.Format(CultureInfo.InvariantCulture, "OpenProcess failed: {0}", Marshal.GetLastWin32Error()));
        }

        try
        {
            byte[] buffer = BitConverter.GetBytes(value);
            UIntPtr bytesWritten;
            bool ok = WriteProcessMemory(processHandle, new UIntPtr(address), buffer, new UIntPtr((uint)buffer.Length), out bytesWritten);
            if (!ok || bytesWritten.ToUInt64() != (ulong)buffer.Length)
            {
                throw new InvalidOperationException(
                    string.Format(
                        CultureInfo.InvariantCulture,
                        "WriteProcessMemory failed: ok={0} bytes={1} error={2}",
                        ok,
                        bytesWritten.ToUInt64(),
                        Marshal.GetLastWin32Error()));
            }
        }
        finally
        {
            CloseHandle(processHandle);
        }
    }

    private static List<object> FindPowersForTarget(ClrRuntime runtime, ClrObject target)
    {
        List<object> directPowers = FindPowersInCreatureList(target);
        if (directPowers.Count > 0)
        {
            return directPowers;
        }

        List<object> powers = new List<object>();
        if (target.IsNull)
        {
            return powers;
        }

        foreach (ClrObject obj in runtime.Heap.EnumerateObjects())
        {
            if (obj.IsNull || obj.Type == null || obj.Type.Name == null)
            {
                continue;
            }

            if (!obj.Type.Name.StartsWith("MegaCrit.Sts2.Core.Models.Powers.", StringComparison.Ordinal))
            {
                continue;
            }

            ClrObject powerTarget = ReadNamedObjectField(obj, "_target");
            if (powerTarget.IsNull || powerTarget.Address != target.Address)
            {
                continue;
            }

            powers.Add(BuildPowerPayload(obj));
        }

        return powers;
    }

    private static List<object> FindPowersInCreatureList(ClrObject creature)
    {
        List<object> powers = new List<object>();
        if (creature.IsNull)
        {
            return powers;
        }

        ClrObject powerList = ReadNamedObjectField(creature, "_powers");
        if (powerList.IsNull)
        {
            return powers;
        }

        int size = ReadIntField(powerList, "_size");
        if (size <= 0)
        {
            return powers;
        }

        ClrObject items = ReadNamedObjectField(powerList, "_items");
        if (items.IsNull || items.Type == null || !items.Type.IsArray)
        {
            return powers;
        }

        Microsoft.Diagnostics.Runtime.ClrArray array = items.AsArray();
        int count = Math.Min(size, array.Length);
        for (int index = 0; index < count; index++)
        {
            ClrObject power = array.GetObjectValue(index);
            if (power.IsNull || power.Type == null || power.Type.Name == null)
            {
                continue;
            }

            if (!power.Type.Name.StartsWith("MegaCrit.Sts2.Core.Models.Powers.", StringComparison.Ordinal))
            {
                continue;
            }

            powers.Add(BuildPowerPayload(power));
        }

        return powers;
    }

    private static Dictionary<string, object> BuildPowerPayload(ClrObject power)
    {
        return new Dictionary<string, object>
        {
            ["address"] = FormatAddress(power),
            ["type"] = power.Type == null ? "" : power.Type.Name,
            ["amount"] = ReadIntField(power, "_amount"),
        };
    }

    private static ClrObject FirstObject(Dictionary<string, List<ClrObject>> matches, string typeName)
    {
        List<ClrObject> list;
        if (!matches.TryGetValue(typeName, out list) || list.Count == 0)
        {
            return default(ClrObject);
        }

        return list[0];
    }

    private static List<ClrObject> FindLiveEnemies(Dictionary<string, List<ClrObject>> matches, ClrObject combatState)
    {
        List<ClrObject> creatures;
        if (!matches.TryGetValue("MegaCrit.Sts2.Core.Entities.Creatures.Creature", out creatures))
        {
            return new List<ClrObject>();
        }

        return creatures
            .Where(
                enemy =>
                {
                    if (enemy.IsNull || ReadIntField(enemy, "<Side>k__BackingField") != 2)
                    {
                        return false;
                    }

                    if (combatState.IsNull)
                    {
                        return ReadIntField(enemy, "_currentHp") >= 0;
                    }

                    ClrObject enemyCombatState = ReadNamedObjectField(enemy, "<CombatState>k__BackingField");
                    return !enemyCombatState.IsNull && enemyCombatState.Address == combatState.Address;
                })
            .ToList();
    }

    private static void DumpRoots(ClrRuntime runtime, Dictionary<string, List<ClrObject>> matches)
    {
        Dictionary<ulong, string> targets = new Dictionary<ulong, string>();
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Runs.RunManager", _ => true);
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Combat.CombatManager", _ => true);
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Combat.CombatState", _ => true);
        AddTargets(targets, matches, "MegaCrit.sts2.Core.Nodes.TopBar.NTopBarGold", _ => true);
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Entities.Players.Player", obj => ReadIntField(obj, "_gold") == 453);

        Console.WriteLine("roots");
        foreach (ClrRoot root in runtime.Heap.EnumerateRoots())
        {
            string label;
            if (!targets.TryGetValue(root.Object, out label))
            {
                continue;
            }

            Console.WriteLine(
                "  root object=0x{0:x} target={1} rootAddr=0x{2:x} kind={3} interior={4} pinned={5}",
                root.Object,
                label,
                root.Address,
                root.RootKind,
                root.IsInterior,
                root.IsPinned);
        }

        Console.WriteLine();
    }

    private static void DumpBackrefs(ClrRuntime runtime, Dictionary<string, List<ClrObject>> matches)
    {
        Dictionary<ulong, string> targets = new Dictionary<ulong, string>();
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Runs.RunManager", _ => true);
        AddTargets(targets, matches, "MegaCrit.Sts2.Core.Combat.CombatManager", _ => true);

        Console.WriteLine("backrefs");
        foreach (ClrObject obj in runtime.Heap.EnumerateObjects())
        {
            if (obj.IsNull || obj.Type == null || obj.Type.Name == null)
            {
                continue;
            }

            foreach (ClrInstanceField field in obj.Type.Fields)
            {
                ClrObject child = ReadInstanceObject(obj, field);
                string label;
                if (child.IsNull || !targets.TryGetValue(child.Address, out label))
                {
                    continue;
                }

                Console.WriteLine(
                    "  referrer=0x{0:x} {1} field={2} -> 0x{3:x} {4}",
                    obj.Address,
                    obj.Type.Name,
                    field.Name,
                    child.Address,
                    label);
            }
        }

        Console.WriteLine();
    }

    private static void AddTargets(
        Dictionary<ulong, string> targets,
        Dictionary<string, List<ClrObject>> matches,
        string typeName,
        Func<ClrObject, bool> predicate)
    {
        List<ClrObject> list;
        if (!matches.TryGetValue(typeName, out list))
        {
            return;
        }

        foreach (ClrObject obj in list)
        {
            if (obj.IsNull || !predicate(obj))
            {
                continue;
            }

            targets[obj.Address] = typeName;
        }
    }
}
