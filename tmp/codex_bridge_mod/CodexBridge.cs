using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using Godot;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Hooks;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;

namespace CodexBridge;

[ModInitializer("Initialize")]
public static class CodexBridgeMod
{
    private const string PipeName = "sts2_codex_bridge";
    private static readonly Queue<BridgeCommand> PendingCombat = new();
    private static readonly Queue<BridgeCommand> PendingRun = new();
    private static readonly object PendingCombatLock = new object();
    private static readonly object PendingRunLock = new object();
    private static Callable? _processCallable;
    private static bool _processConnected;
    private static bool _processReadyLogged;
    private static bool _processBusy;
    private static int _initialized;
    private static readonly Lazy<Dictionary<string, Type>> CardTypeIndex = new(() => BuildTypeIndex("MegaCrit.Sts2.Core.Models.Cards", typeof(CardModel), stripSuffix: "Card", requireParameterlessConstructor: false));
    private static readonly Lazy<Dictionary<string, Type>> PowerTypeIndex = new(() => BuildTypeIndex("MegaCrit.Sts2.Core.Models.Powers", typeof(PowerModel), stripSuffix: "Power", requireParameterlessConstructor: true));
    private static readonly Lazy<Dictionary<string, Type>> RelicTypeIndex = new(() => BuildTypeIndex("MegaCrit.Sts2.Core.Models.Relics", typeof(RelicModel), stripSuffix: "Relic", requireParameterlessConstructor: true));
    private static readonly MethodInfo CombatCreateCardMethod = ResolveCreateCardMethod(typeof(CombatState));
    private static readonly MethodInfo RunCreateCardMethod = ResolveCreateCardMethod(typeof(RunState));
    private static readonly MethodInfo RunRegisterCardMethod = ResolveRunAddCardMethod();
    private static readonly MethodInfo GenericRelicObtainMethod = ResolveGenericRelicObtainMethod();

    public static void Initialize()
    {
        if (Interlocked.Exchange(ref _initialized, 1) == 1)
        {
            return;
        }

        Log.Warn("CodexBridge initializing");
        InstallPump();

        Thread thread = new Thread(ServerLoop)
        {
            IsBackground = true,
            Name = "CodexBridgePipe",
        };
        thread.Start();
    }

    private static void InstallPump()
    {
        if (_processConnected)
        {
            return;
        }

        SceneTree? tree = Engine.GetMainLoop() as SceneTree;
        if (tree == null)
        {
            Log.Warn("CodexBridge could not install pump: root scene missing");
            return;
        }

        _processCallable = Callable.From(ProcessBridgeFrame);
        tree.Connect(SceneTree.SignalName.ProcessFrame, _processCallable.Value, (uint)GodotObject.ConnectFlags.ReferenceCounted);
        _processConnected = true;
        Log.Warn("CodexBridge pump installed");
    }

    private static BridgeCommand? TryDequeueCombatCommand()
    {
        lock (PendingCombatLock)
        {
            if (PendingCombat.Count == 0)
            {
                return null;
            }

            return PendingCombat.Dequeue();
        }
    }

    private static BridgeCommand? TryDequeueRunCommand()
    {
        lock (PendingRunLock)
        {
            if (PendingRun.Count == 0)
            {
                return null;
            }

            return PendingRun.Dequeue();
        }
    }

    private static async System.Threading.Tasks.Task HandleCommandAsync(CombatState state, BridgeCommand command)
    {
        string action = command.Action?.Trim() ?? string.Empty;
        if (string.Equals(action, "apply_power", StringComparison.OrdinalIgnoreCase))
        {
            await ApplyPowerAsync(state, command);
            return;
        }

        if (string.Equals(action, "add_card_to_deck", StringComparison.OrdinalIgnoreCase))
        {
            await AddCardToDeckAsync((RunState)state.RunState, command);
            return;
        }

        if (string.Equals(action, "add_card_to_hand", StringComparison.OrdinalIgnoreCase))
        {
            await AddCardToHandAsync(state, command);
            return;
        }

        if (string.Equals(action, "replace_master_deck", StringComparison.OrdinalIgnoreCase))
        {
            await ReplaceMasterDeckAsync((RunState)state.RunState, command);
            return;
        }

        if (string.Equals(action, "obtain_relic", StringComparison.OrdinalIgnoreCase))
        {
            await ObtainRelicAsync((RunState)state.RunState, command);
            return;
        }

        Log.Warn("CodexBridge unsupported action: " + command.Action);
    }

    private static async System.Threading.Tasks.Task HandleRunCommandAsync(RunState state, BridgeCommand command)
    {
        string action = command.Action?.Trim() ?? string.Empty;
        if (string.Equals(action, "add_card_to_deck", StringComparison.OrdinalIgnoreCase))
        {
            await AddCardToDeckAsync(state, command);
            return;
        }

        if (string.Equals(action, "replace_master_deck", StringComparison.OrdinalIgnoreCase))
        {
            await ReplaceMasterDeckAsync(state, command);
            return;
        }

        if (string.Equals(action, "obtain_relic", StringComparison.OrdinalIgnoreCase))
        {
            await ObtainRelicAsync(state, command);
            return;
        }

        Log.Warn("CodexBridge run action ignored until combat: " + action);
    }

    private static async System.Threading.Tasks.Task ApplyPowerAsync(CombatState state, BridgeCommand command)
    {
        Creature? target = ResolveTarget(state, command);
        if (target == null)
        {
            Log.Warn("CodexBridge target not found for action apply_power");
            return;
        }

        Player? firstPlayer = TryGetFirstPlayer(state);
        Creature source = firstPlayer == null ? target : firstPlayer.Creature;
        PowerModel power = CreatePower(command.PowerType);

        await PowerCmd.Apply(power, target, command.Amount, source, null, false);
        Log.Warn("CodexBridge applied " + command.PowerType + " amount=" + command.Amount + " target=" + command.Target);
    }

    private static System.Threading.Tasks.Task AddCardToDeckAsync(RunState state, BridgeCommand command)
    {
        Player player = ResolvePlayer(state);
        int count = Math.Max(1, command.Count);
        var preparedCards = new List<CardModel>(count);
        for (int index = 0; index < count; index++)
        {
            CardModel card = CreateRunCard(state, player, command.CardType);
            preparedCards.Add(PrepareCardForDeck(state, card));
        }

        foreach (CardModel card in preparedCards)
        {
            CommitCardToDeck(state, player, card, silent: false);
        }

        Log.Warn("CodexBridge added " + count + " " + command.CardType + " card(s) to deck");
        return System.Threading.Tasks.Task.CompletedTask;
    }

    private static async System.Threading.Tasks.Task AddCardToHandAsync(CombatState state, BridgeCommand command)
    {
        Player player = ResolvePlayer(state);
        int count = Math.Max(1, command.Count);
        for (int index = 0; index < count; index++)
        {
            CardModel card = CreateCombatCard(state, player, command.CardType);
            await CardPileCmd.AddGeneratedCardToCombat(card, PileType.Hand, true, CardPilePosition.Random);
        }

        Log.Warn("CodexBridge added " + count + " " + command.CardType + " card(s) to hand");
    }

    private static async System.Threading.Tasks.Task ObtainRelicAsync(RunState state, BridgeCommand command)
    {
        Player player = ResolvePlayer(state);
        Type relicModelType = ResolveIndexedType(RelicTypeIndex.Value, command.RelicType, "relic");
        int count = Math.Max(1, command.Count);
        for (int index = 0; index < count; index++)
        {
            Task task = (Task)(GenericRelicObtainMethod.MakeGenericMethod(relicModelType).Invoke(null, new object[] { player }) ?? throw new InvalidOperationException("RelicCmd.Obtain returned null"));
            await task;
        }

        Log.Warn("CodexBridge obtained " + count + " " + command.RelicType + " relic(s)");
    }

    private static System.Threading.Tasks.Task ReplaceMasterDeckAsync(RunState state, BridgeCommand command)
    {
        return ReplaceMasterDeckAsyncImpl(state, command);
    }

    private static async System.Threading.Tasks.Task ReplaceMasterDeckAsyncImpl(RunState state, BridgeCommand command)
    {
        Player player = ResolvePlayer(state);
        var existingCards = new List<CardModel>(player.Deck.Cards);
        int replacementCount = command.Count > 0 ? command.Count : existingCards.Count;
        var replacementCards = new List<CardModel>(replacementCount);
        for (int index = 0; index < replacementCount; index++)
        {
            CardModel card = CreateRunCard(state, player, command.CardType);
            replacementCards.Add(PrepareCardForDeck(state, card));
        }

        if (existingCards.Count > 0)
        {
            await CardPileCmd.RemoveFromDeck(existingCards, false);
        }

        foreach (CardModel card in replacementCards)
        {
            CommitCardToDeck(state, player, card, silent: false);
        }

        Log.Warn("CodexBridge replaced master deck with " + replacementCount + " " + command.CardType + " card(s)");
    }

    private static Creature? ResolveTarget(CombatState state, BridgeCommand command)
    {
        if (string.Equals(command.Target, "player", StringComparison.OrdinalIgnoreCase))
        {
            Player? player = TryGetFirstPlayer(state);
            return player == null ? null : player.Creature;
        }

        if (string.Equals(command.Target, "enemy", StringComparison.OrdinalIgnoreCase))
        {
            int index = Math.Max(0, command.EnemyIndex);
            return index < state.Enemies.Count ? state.Enemies[index] : null;
        }

        return null;
    }

    private static Player ResolvePlayer(CombatState state)
    {
        Player? player = TryGetFirstPlayer(state);
        if (player == null)
        {
            throw new InvalidOperationException("player not found");
        }

        return player;
    }

    private static Player ResolvePlayer(RunState state)
    {
        Player? player = state.Players.Count > 0 ? state.Players[0] : null;
        if (player == null)
        {
            throw new InvalidOperationException("player not found");
        }

        return player;
    }

    private static CardModel CreateCombatCard(CombatState state, Player owner, string cardType)
    {
        return CreateCard(state, owner, cardType, CombatCreateCardMethod);
    }

    private static CardModel CreateRunCard(RunState state, Player owner, string cardType)
    {
        return CreateCard(state, owner, cardType, RunCreateCardMethod);
    }

    private static CardModel PrepareCardForDeck(RunState state, CardModel card)
    {
        if (!Hook.ShouldAddToDeck(state, card, out AbstractModel? preventer))
        {
            if (preventer != null)
            {
                preventer.AfterAddToDeckPrevented(card).GetAwaiter().GetResult();
            }

            throw new InvalidOperationException("card add prevented by hook: " + card.GetType().Name);
        }

        card = Hook.ModifyCardBeingAddedToDeck(state, card, out _);
        card.FloorAddedToDeck = state.TotalFloor;
        return card;
    }

    private static void CommitCardToDeck(RunState state, Player player, CardModel card, bool silent)
    {
        RunRegisterCardMethod.Invoke(state, new object[] { card });
        player.Deck.AddInternal(card, player.Deck.Cards.Count, silent);
        player.Deck.InvokeContentsChanged();
        player.Deck.InvokeCardAddFinished();
    }

    private static string NormalizeCardType(string cardType)
    {
        return NormalizeLookup(cardType);
    }

    private static CardModel CreateCard(object state, Player owner, string cardType, MethodInfo genericCreateCardMethod)
    {
        Type cardModelType = ResolveIndexedType(CardTypeIndex.Value, cardType, "card");
        object? result = genericCreateCardMethod.MakeGenericMethod(cardModelType).Invoke(state, new object[] { owner });
        if (result is not CardModel card)
        {
            throw new InvalidOperationException("bridge card factory returned an unexpected result for " + cardModelType.FullName);
        }

        return card;
    }

    private static PowerModel CreatePower(string powerType)
    {
        Type powerModelType = ResolveIndexedType(PowerTypeIndex.Value, powerType, "power");
        object? instance = Activator.CreateInstance(powerModelType);
        if (instance is not PowerModel power)
        {
            throw new InvalidOperationException("failed to instantiate power: " + powerType);
        }

        return power;
    }

    private static Type ResolveIndexedType(IReadOnlyDictionary<string, Type> index, string requestedType, string category)
    {
        string key = NormalizeLookup(requestedType);
        if (string.IsNullOrWhiteSpace(key))
        {
            throw new InvalidOperationException("missing " + category + " type");
        }

        if (index.TryGetValue(key, out Type? resolved))
        {
            return resolved;
        }

        string available = string.Join(", ", index.Values.Select(type => type.Name).Distinct(StringComparer.Ordinal).OrderBy(name => name, StringComparer.Ordinal).Take(24));
        throw new InvalidOperationException("unsupported " + category + " type: " + requestedType + "; available=" + available);
    }

    private static Dictionary<string, Type> BuildTypeIndex(string namespacePrefix, Type baseType, string stripSuffix, bool requireParameterlessConstructor)
    {
        var aliases = new Dictionary<string, Type>(StringComparer.OrdinalIgnoreCase);
        var collisions = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (Type type in typeof(CardModel).Assembly.GetTypes()
                     .Where(type => type.Namespace == namespacePrefix)
                     .Where(type => !type.IsNested && !type.IsAbstract)
                     .Where(type => !type.Name.StartsWith("<", StringComparison.Ordinal))
                     .Where(baseType.IsAssignableFrom)
                     .Where(type => !requireParameterlessConstructor || type.GetConstructor(Type.EmptyTypes) != null))
        {
            foreach (string alias in BuildAliases(type, stripSuffix))
            {
                if (collisions.Contains(alias))
                {
                    continue;
                }

                if (aliases.TryGetValue(alias, out Type? existing) && existing != type)
                {
                    aliases.Remove(alias);
                    collisions.Add(alias);
                    continue;
                }

                aliases[alias] = type;
            }
        }

        return aliases;
    }

    private static IEnumerable<string> BuildAliases(Type type, string stripSuffix)
    {
        string fullName = type.FullName ?? type.Name;
        yield return NormalizeLookup(fullName);
        yield return NormalizeLookup(type.Name);
        if (type.Name.EndsWith(stripSuffix, StringComparison.OrdinalIgnoreCase) && type.Name.Length > stripSuffix.Length)
        {
            yield return NormalizeLookup(type.Name.Substring(0, type.Name.Length - stripSuffix.Length));
        }
    }

    private static string NormalizeLookup(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        return new string(value.Where(char.IsLetterOrDigit).Select(char.ToLowerInvariant).ToArray());
    }

    private static MethodInfo ResolveCreateCardMethod(Type stateType)
    {
        MethodInfo? method = stateType
            .GetMethods(BindingFlags.Public | BindingFlags.Instance)
            .SingleOrDefault(
                item => item.Name == "CreateCard"
                    && item.IsGenericMethodDefinition
                    && item.GetParameters().Length == 1
                    && item.GetParameters()[0].ParameterType == typeof(Player));
        if (method == null)
        {
            throw new InvalidOperationException("CreateCard<T>(Player) not found on " + stateType.FullName);
        }

        return method;
    }

    private static MethodInfo ResolveRunAddCardMethod()
    {
        MethodInfo? method = typeof(RunState)
            .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .SingleOrDefault(
                item => item.Name == "AddCard"
                    && item.GetParameters().Length == 1
                    && item.GetParameters()[0].ParameterType == typeof(CardModel));
        if (method == null)
        {
            throw new InvalidOperationException("RunState.AddCard(CardModel) not found");
        }

        return method;
    }

    private static MethodInfo ResolveGenericRelicObtainMethod()
    {
        MethodInfo? method = typeof(RelicCmd)
            .GetMethods(BindingFlags.Public | BindingFlags.Static)
            .SingleOrDefault(
                item => item.Name == "Obtain"
                    && item.IsGenericMethodDefinition
                    && item.GetParameters().Length == 1
                    && item.GetParameters()[0].ParameterType == typeof(Player));
        if (method == null)
        {
            throw new InvalidOperationException("RelicCmd.Obtain<T>(Player) not found");
        }

        return method;
    }

    private static Player? TryGetFirstPlayer(CombatState state)
    {
        return state.Players.Count > 0 ? state.Players[0] : null;
    }

    private static void ServerLoop()
    {
        while (true)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(PipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.None);
                pipe.WaitForConnection();
                using var reader = new StreamReader(pipe);
                using var writer = new StreamWriter(pipe) { AutoFlush = true };
                string? line = reader.ReadLine();
        if (string.IsNullOrWhiteSpace(line))
        {
            writer.WriteLine("ok=false;error=empty_request");
            continue;
        }

                BridgeCommand? command = ParseCommand(line);
                if (command == null)
                {
                    writer.WriteLine("ok=false;error=invalid_request");
                    continue;
                }

                if (RequiresRunState(command.Action))
                {
                    lock (PendingRunLock)
                    {
                        PendingRun.Enqueue(command);
                    }
                }
                else
                {
                    lock (PendingCombatLock)
                    {
                        PendingCombat.Enqueue(command);
                    }
                }
                writer.WriteLine("ok=true;status=queued");
            }
            catch (Exception ex)
            {
                try
                {
                    Log.Error("CodexBridge pipe failure: " + ex);
                }
                catch
                {
                }

                Thread.Sleep(250);
            }
        }
    }

    private sealed class BridgeCommand
    {
        public string Action { get; set; } = string.Empty;
        public string Target { get; set; } = "player";
        public string PowerType { get; set; } = string.Empty;
        public string CardType { get; set; } = string.Empty;
        public string RelicType { get; set; } = string.Empty;
        public decimal Amount { get; set; }
        public int EnemyIndex { get; set; }
        public int Count { get; set; }
    }

    private static bool RequiresRunState(string action)
    {
        return string.Equals(action, "add_card_to_deck", StringComparison.OrdinalIgnoreCase)
            || string.Equals(action, "replace_master_deck", StringComparison.OrdinalIgnoreCase)
            || string.Equals(action, "obtain_relic", StringComparison.OrdinalIgnoreCase);
    }

    private static BridgeCommand? ParseCommand(string line)
    {
        var command = new BridgeCommand();
        bool sawAction = false;
        string[] parts = line.Split(';');
        foreach (string rawPart in parts)
        {
            if (string.IsNullOrWhiteSpace(rawPart))
            {
                continue;
            }

            string[] kv = rawPart.Split(new[] { '=' }, 2);
            if (kv.Length != 2)
            {
                continue;
            }

            string key = kv[0].Trim();
            string value = kv[1].Trim();
            switch (key)
            {
                case "action":
                    command.Action = value;
                    sawAction = true;
                    break;
                case "target":
                    command.Target = value;
                    break;
                case "power_type":
                    command.PowerType = value;
                    break;
                case "card_type":
                    command.CardType = value;
                    break;
                case "relic_type":
                    command.RelicType = value;
                    break;
                case "amount":
                    command.Amount = ParseDecimal(value);
                    break;
                case "enemy_index":
                    command.EnemyIndex = ParseInt(value);
                    break;
                case "count":
                    command.Count = ParseInt(value);
                    break;
            }
        }

        return sawAction ? command : null;
    }

    private static decimal ParseDecimal(string value)
    {
        return decimal.TryParse(value, out decimal parsed) ? parsed : 0m;
    }

    private static int ParseInt(string value)
    {
        return int.TryParse(value, out int parsed) ? parsed : 0;
    }

    private static void ProcessBridgeFrame()
    {
        if (!_processReadyLogged)
        {
            Log.Warn("CodexBridge pump ready");
            _processReadyLogged = true;
        }

        if (_processBusy)
        {
            return;
        }

        try
        {
            if (TryProcessCombatCommand())
            {
                return;
            }

            TryProcessRunCommand();
        }
        catch (Exception ex)
        {
            Log.Error("CodexBridge pump failure: " + ex);
        }
    }

    private static bool TryProcessCombatCommand()
    {
        BridgeCommand? command = TryDequeueCombatCommand();
        if (command == null)
        {
            return false;
        }

        CombatManager? combatManager = CombatManager.Instance;
        CombatState? state = combatManager?.DebugOnlyGetState();
        if (state == null || combatManager == null || !IsCombatSafe(combatManager))
        {
            lock (PendingCombatLock)
            {
                PendingCombat.Enqueue(command);
            }
            return false;
        }

        _processBusy = true;
        Log.Warn("CodexBridge executing combat action: " + command.Action);
        _ = CompleteCombatCommandAsync(state, command);
        return true;
    }

    private static bool TryProcessRunCommand()
    {
        BridgeCommand? command = TryDequeueRunCommand();
        if (command == null)
        {
            return false;
        }

        CombatManager? combatManager = CombatManager.Instance;
        if (combatManager != null && combatManager.IsInProgress)
        {
            lock (PendingRunLock)
            {
                PendingRun.Enqueue(command);
            }
            return false;
        }

        RunManager? runManager = RunManager.Instance;
        RunState? state = runManager?.DebugOnlyGetState();
        if (state == null)
        {
            lock (PendingRunLock)
            {
                PendingRun.Enqueue(command);
            }
            return false;
        }

        _processBusy = true;
        Log.Warn("CodexBridge executing run action: " + command.Action);
        _ = CompleteRunCommandAsync(state, command);
        return true;
    }

    private static async System.Threading.Tasks.Task CompleteCombatCommandAsync(CombatState state, BridgeCommand command)
    {
        try
        {
            await HandleCommandAsync(state, command);
            Log.Warn("CodexBridge completed combat action: " + command.Action);
        }
        catch (Exception ex)
        {
            Log.Error("CodexBridge combat action failed: " + ex);
        }
        finally
        {
            _processBusy = false;
        }
    }

    private static async System.Threading.Tasks.Task CompleteRunCommandAsync(RunState state, BridgeCommand command)
    {
        try
        {
            await HandleRunCommandAsync(state, command);
            Log.Warn("CodexBridge completed run action: " + command.Action);
        }
        catch (Exception ex)
        {
            Log.Error("CodexBridge run action failed: " + ex);
        }
        finally
        {
            _processBusy = false;
        }
    }

    private static bool IsCombatSafe(CombatManager combatManager)
    {
        return combatManager.IsInProgress
            && combatManager.IsPlayPhase
            && !combatManager.PlayerActionsDisabled
            && !combatManager.IsPaused
            && !combatManager.IsEnemyTurnStarted
            && !combatManager.EndingPlayerTurnPhaseOne
            && !combatManager.EndingPlayerTurnPhaseTwo
            && !combatManager.IsEnding
            && !combatManager.IsOverOrEnding;
    }
}
