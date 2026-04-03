using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;

internal static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            Console.Error.WriteLine("usage: type_probe <assembly-path> <type-name> [member keyword ...] | type_probe --search <assembly-path> <keyword ...>");
            return 1;
        }

        try
        {
            if (string.Equals(args[0], "--types", StringComparison.Ordinal))
            {
                if (args.Length < 3)
                {
                    Console.Error.WriteLine("usage: type_probe --types <assembly-path> <namespace-prefix> [--assignable-to <type-name>]");
                    return 1;
                }

                string listAssemblyPath = Path.GetFullPath(args[1]);
                string namespacePrefix = args[2];
                string assignableTo = string.Empty;
                if (args.Length >= 5 && string.Equals(args[3], "--assignable-to", StringComparison.Ordinal))
                {
                    assignableTo = args[4];
                }

                Console.WriteLine(JsonSerializer.Serialize(ListTypes(listAssemblyPath, namespacePrefix, assignableTo)));
                return 0;
            }

            if (string.Equals(args[0], "--search", StringComparison.Ordinal))
            {
                if (args.Length < 3)
                {
                    Console.Error.WriteLine("usage: type_probe --search <assembly-path> <keyword ...>");
                    return 1;
                }

                string searchAssemblyPath = Path.GetFullPath(args[1]);
                string[] keywords = args.Skip(2).ToArray();
                Console.WriteLine(JsonSerializer.Serialize(SearchSignatures(searchAssemblyPath, keywords)));
                return 0;
            }

            if (args.Length < 2)
            {
                Console.Error.WriteLine("usage: type_probe <assembly-path> <type-name> [member keyword ...]");
                return 1;
            }

            string assemblyPath = Path.GetFullPath(args[0]);
            string typeName = args[1];
            string[] memberKeywords = args.Skip(2).ToArray();
            Console.WriteLine(JsonSerializer.Serialize(DescribeType(assemblyPath, typeName, memberKeywords)));
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

    private static Dictionary<string, object> DescribeType(string assemblyPath, string typeName, string[] memberKeywords)
    {
        string assemblyDir = Path.GetDirectoryName(assemblyPath) ?? Directory.GetCurrentDirectory();
        var loadContext = new AssemblyLoadContext("type_probe", isCollectible: true);
        loadContext.Resolving += (_, name) =>
        {
            string candidate = Path.Combine(assemblyDir, name.Name + ".dll");
            return File.Exists(candidate) ? loadContext.LoadFromAssemblyPath(candidate) : null;
        };

        try
        {
            Assembly assembly = loadContext.LoadFromAssemblyPath(assemblyPath);
            Type type = assembly.GetType(typeName, throwOnError: true)!;
            return new Dictionary<string, object>
            {
                ["assembly_path"] = assemblyPath,
                ["type_name"] = type.FullName ?? typeName,
                ["base_type"] = type.BaseType == null ? string.Empty : FriendlyName(type.BaseType),
                ["attributes"] = type.GetCustomAttributesData().Select(DescribeAttribute).Cast<object>().ToList(),
                ["constructors"] = type
                    .GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                    .Where(ctor => MatchesMember(ctor, memberKeywords))
                    .Select(ctor => SafeDescribeMethodBase(ctor, "constructor"))
                    .Where(item => item != null)
                    .Cast<object>()
                    .ToList(),
                ["methods"] = type
                    .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly)
                    .Where(method => !method.IsSpecialName)
                    .Where(method => MatchesMember(method, memberKeywords))
                    .Select(method => SafeDescribeMethodBase(method, "method"))
                    .Where(item => item != null)
                    .Cast<object>()
                    .ToList(),
                ["properties"] = type
                    .GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly)
                    .Where(property => MatchesMember(property, memberKeywords))
                    .Select(SafeDescribeProperty)
                    .Where(item => item != null)
                    .Cast<object>()
                    .ToList(),
                ["fields"] = type
                    .GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly)
                    .Where(field => MatchesMember(field, memberKeywords))
                    .Select(SafeDescribeField)
                    .Where(item => item != null)
                    .Cast<object>()
                    .ToList(),
            };
        }
        finally
        {
            loadContext.Unload();
        }
    }

    private static Dictionary<string, object> SearchSignatures(string assemblyPath, string[] keywords)
    {
        string assemblyDir = Path.GetDirectoryName(assemblyPath) ?? Directory.GetCurrentDirectory();
        var loadContext = new AssemblyLoadContext("type_probe_search", isCollectible: true);
        loadContext.Resolving += (_, name) =>
        {
            string candidate = Path.Combine(assemblyDir, name.Name + ".dll");
            return File.Exists(candidate) ? loadContext.LoadFromAssemblyPath(candidate) : null;
        };

        try
        {
            Assembly assembly = loadContext.LoadFromAssemblyPath(assemblyPath);
            var loweredKeywords = keywords.Select(keyword => keyword.ToLowerInvariant()).ToArray();
            var matches = new List<object>();
            foreach (Type type in SafeGetTypes(assembly).OrderBy(type => type.FullName, StringComparer.Ordinal))
            {
                string typeName = type.FullName ?? type.Name;
                bool typeMatched = Matches(typeName, loweredKeywords);
                var memberMatches = new List<object>();

                foreach (FieldInfo field in type.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
                {
                    string? signature = SafeFormatField(field);
                    if (signature == null)
                    {
                        continue;
                    }
                    if (Matches(signature, loweredKeywords) || Matches(field.Name, loweredKeywords))
                    {
                        memberMatches.Add(new Dictionary<string, object> { ["kind"] = "field", ["signature"] = signature });
                    }
                }

                foreach (PropertyInfo property in type.GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
                {
                    string? signature = SafeFormatProperty(property);
                    if (signature == null)
                    {
                        continue;
                    }
                    if (Matches(signature, loweredKeywords) || Matches(property.Name, loweredKeywords))
                    {
                        memberMatches.Add(new Dictionary<string, object> { ["kind"] = "property", ["signature"] = signature });
                    }
                }

                foreach (ConstructorInfo ctor in type.GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
                {
                    string? signature = SafeFormatMethodBase(ctor);
                    if (signature == null)
                    {
                        continue;
                    }
                    if (Matches(signature, loweredKeywords) || Matches(ctor.Name, loweredKeywords))
                    {
                        memberMatches.Add(new Dictionary<string, object> { ["kind"] = "constructor", ["signature"] = signature });
                    }
                }

                foreach (MethodInfo method in type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
                {
                    if (method.IsSpecialName)
                    {
                        continue;
                    }

                    string? signature = SafeFormatMethodBase(method);
                    if (signature == null)
                    {
                        continue;
                    }
                    if (Matches(signature, loweredKeywords) || Matches(method.Name, loweredKeywords))
                    {
                        memberMatches.Add(new Dictionary<string, object> { ["kind"] = "method", ["signature"] = signature });
                    }
                }

                if (typeMatched || memberMatches.Count > 0)
                {
                    matches.Add(
                        new Dictionary<string, object>
                        {
                            ["type_name"] = typeName,
                            ["type_matched"] = typeMatched,
                            ["members"] = memberMatches,
                        });
                }
            }

            return new Dictionary<string, object>
            {
                ["assembly_path"] = assemblyPath,
                ["keywords"] = keywords.Cast<object>().ToList(),
                ["matches"] = matches,
            };
        }
        finally
        {
            loadContext.Unload();
        }
    }

    private static Dictionary<string, object> ListTypes(string assemblyPath, string namespacePrefix, string assignableTo)
    {
        string assemblyDir = Path.GetDirectoryName(assemblyPath) ?? Directory.GetCurrentDirectory();
        var loadContext = new AssemblyLoadContext("type_probe_list_types", isCollectible: true);
        loadContext.Resolving += (_, name) =>
        {
            string candidate = Path.Combine(assemblyDir, name.Name + ".dll");
            return File.Exists(candidate) ? loadContext.LoadFromAssemblyPath(candidate) : null;
        };

        try
        {
            Assembly assembly = loadContext.LoadFromAssemblyPath(assemblyPath);
            Type? assignableType = string.IsNullOrWhiteSpace(assignableTo) ? null : assembly.GetType(assignableTo, throwOnError: true, ignoreCase: false);
            var entries = SafeGetTypes(assembly)
                .Where(type => type.Namespace != null)
                .Where(type => string.Equals(type.Namespace, namespacePrefix, StringComparison.Ordinal))
                .Where(type => !type.IsNested)
                .Where(type => !type.Name.StartsWith("<", StringComparison.Ordinal))
                .Where(type => assignableType == null || assignableType.IsAssignableFrom(type))
                .OrderBy(type => type.FullName, StringComparer.Ordinal)
                .Select(
                    type => (object)new Dictionary<string, object>
                    {
                        ["type_name"] = type.FullName ?? type.Name,
                        ["base_type"] = type.BaseType == null ? string.Empty : FriendlyName(type.BaseType),
                        ["is_abstract"] = type.IsAbstract,
                        ["has_parameterless_constructor"] = type.GetConstructor(Type.EmptyTypes) != null,
                    })
                .ToList();

            return new Dictionary<string, object>
            {
                ["assembly_path"] = assemblyPath,
                ["namespace_prefix"] = namespacePrefix,
                ["assignable_to"] = assignableTo,
                ["types"] = entries,
            };
        }
        finally
        {
            loadContext.Unload();
        }
    }

    private static Dictionary<string, object> DescribeMethodBase(MethodBase method, string kind)
    {
        Type? returnType = method is MethodInfo info ? info.ReturnType : null;
        return new Dictionary<string, object>
        {
            ["name"] = method.Name,
            ["kind"] = kind,
            ["return_type"] = returnType == null ? string.Empty : FriendlyName(returnType),
            ["is_static"] = method.IsStatic,
            ["parameters"] = method.GetParameters()
                .Select(
                    parameter => (object)new Dictionary<string, object>
                    {
                        ["name"] = parameter.Name ?? string.Empty,
                        ["type"] = FriendlyName(parameter.ParameterType),
                    })
                .ToList(),
        };
    }

    private static Dictionary<string, object>? SafeDescribeMethodBase(MethodBase method, string kind)
    {
        try
        {
            return DescribeMethodBase(method, kind);
        }
        catch
        {
            return null;
        }
    }

    private static string DescribeAttribute(CustomAttributeData attribute)
    {
        string arguments = string.Join(", ", attribute.ConstructorArguments.Select(argument => argument.Value?.ToString() ?? "null"));
        return string.Format("{0}({1})", FriendlyName(attribute.AttributeType), arguments);
    }

    private static Dictionary<string, object>? SafeDescribeProperty(PropertyInfo property)
    {
        try
        {
            return new Dictionary<string, object>
            {
                ["name"] = property.Name,
                ["kind"] = "property",
                ["return_type"] = FriendlyName(property.PropertyType),
                ["is_static"] = (property.GetMethod ?? property.SetMethod)?.IsStatic ?? false,
                ["parameters"] = new List<object>(),
            };
        }
        catch
        {
            return null;
        }
    }

    private static Dictionary<string, object>? SafeDescribeField(FieldInfo field)
    {
        try
        {
            return new Dictionary<string, object>
            {
                ["name"] = field.Name,
                ["kind"] = "field",
                ["return_type"] = FriendlyName(field.FieldType),
                ["is_static"] = field.IsStatic,
                ["parameters"] = new List<object>(),
            };
        }
        catch
        {
            return null;
        }
    }

    private static bool MatchesMember(MemberInfo member, string[] keywords)
    {
        if (keywords.Length == 0)
        {
            return true;
        }

        return Matches(member.Name, keywords) || Matches(member.ToString() ?? string.Empty, keywords);
    }

    private static bool Matches(string text, string[] loweredKeywords)
    {
        if (loweredKeywords.Length == 0)
        {
            return true;
        }

        string lowered = text.ToLowerInvariant();
        return loweredKeywords.Any(lowered.Contains);
    }

    private static IEnumerable<Type> SafeGetTypes(Assembly assembly)
    {
        try
        {
            return assembly.GetTypes().Where(type => type != null)!;
        }
        catch (ReflectionTypeLoadException ex)
        {
            return ex.Types.Where(type => type != null)!.Cast<Type>();
        }
    }

    private static string FormatMethodBase(MethodBase method)
    {
        string returnPrefix = method is MethodInfo info ? FriendlyName(info.ReturnType) + " " : string.Empty;
        string parameters = string.Join(", ", method.GetParameters().Select(FormatParameter));
        string modifiers = FormatVisibility(method.IsPublic, method.IsFamily, method.IsPrivate, method.IsAssembly);
        if (method.IsStatic)
        {
            modifiers = string.IsNullOrEmpty(modifiers) ? "static" : modifiers + " static";
        }

        return string.IsNullOrEmpty(modifiers)
            ? string.Format("{0}{1}({2})", returnPrefix, method.Name, parameters)
            : string.Format("{0} {1}{2}({3})", modifiers, returnPrefix, method.Name, parameters);
    }

    private static string? SafeFormatMethodBase(MethodBase method)
    {
        try
        {
            return FormatMethodBase(method);
        }
        catch
        {
            return null;
        }
    }

    private static string FormatProperty(PropertyInfo property)
    {
        string accessors = string.Join("; ", new[] { property.CanRead ? "get" : null, property.CanWrite ? "set" : null }.Where(value => value != null));
        string modifiers = FormatVisibility(
            (property.GetMethod ?? property.SetMethod)?.IsPublic ?? false,
            (property.GetMethod ?? property.SetMethod)?.IsFamily ?? false,
            (property.GetMethod ?? property.SetMethod)?.IsPrivate ?? false,
            (property.GetMethod ?? property.SetMethod)?.IsAssembly ?? false);
        if ((property.GetMethod ?? property.SetMethod)?.IsStatic ?? false)
        {
            modifiers = string.IsNullOrEmpty(modifiers) ? "static" : modifiers + " static";
        }

        string prefix = string.IsNullOrEmpty(modifiers) ? string.Empty : modifiers + " ";
        return string.Format("{0}{1} {2} {{ {3}; }}", prefix, FriendlyName(property.PropertyType), property.Name, accessors);
    }

    private static string? SafeFormatProperty(PropertyInfo property)
    {
        try
        {
            return FormatProperty(property);
        }
        catch
        {
            return null;
        }
    }

    private static string FormatField(FieldInfo field)
    {
        string modifiers = FormatVisibility(field.IsPublic, field.IsFamily, field.IsPrivate, field.IsAssembly);
        if (field.IsStatic)
        {
            modifiers = string.IsNullOrEmpty(modifiers) ? "static" : modifiers + " static";
        }

        string prefix = string.IsNullOrEmpty(modifiers) ? string.Empty : modifiers + " ";
        return string.Format("{0}{1} {2}", prefix, FriendlyName(field.FieldType), field.Name);
    }

    private static string? SafeFormatField(FieldInfo field)
    {
        try
        {
            return FormatField(field);
        }
        catch
        {
            return null;
        }
    }

    private static string FormatParameter(ParameterInfo parameter)
    {
        string prefix = parameter.IsOut ? "out " : parameter.ParameterType.IsByRef ? "ref " : string.Empty;
        Type type = parameter.ParameterType.IsByRef ? parameter.ParameterType.GetElementType() ?? parameter.ParameterType : parameter.ParameterType;
        return string.Format("{0}{1} {2}", prefix, FriendlyName(type), parameter.Name);
    }

    private static string FormatVisibility(bool isPublic, bool isFamily, bool isPrivate, bool isAssembly)
    {
        if (isPublic)
        {
            return "public";
        }
        if (isFamily)
        {
            return "protected";
        }
        if (isPrivate)
        {
            return "private";
        }
        if (isAssembly)
        {
            return "internal";
        }

        return string.Empty;
    }

    private static string FriendlyName(Type type)
    {
        if (type.IsGenericType)
        {
            string genericName = type.Name;
            int tickIndex = genericName.IndexOf('`');
            if (tickIndex >= 0)
            {
                genericName = genericName.Substring(0, tickIndex);
            }

            return string.Format(
                "{0}.{1}<{2}>",
                type.Namespace,
                genericName,
                string.Join(", ", type.GetGenericArguments().Select(FriendlyName)));
        }

        return type.FullName ?? type.Name;
    }
}
