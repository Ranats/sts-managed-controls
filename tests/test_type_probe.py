from __future__ import annotations

import unittest

from sts_bot.type_probe import ReflectedType, TypeListingResult


class TypeProbeModelsTest(unittest.TestCase):
    def test_reflected_type_from_payload_parses_members(self) -> None:
        reflected = ReflectedType.from_payload(
            {
                "assembly_path": "C:\\game\\sts2.dll",
                "type_name": "MegaCrit.Sts2.Core.Modding.ModManifest",
                "constructors": [
                    {
                        "name": ".ctor",
                        "kind": "constructor",
                        "return_type": "",
                        "is_static": False,
                        "parameters": [{"name": "id", "type": "System.String"}],
                    }
                ],
                "methods": [
                    {
                        "name": "Load",
                        "kind": "method",
                        "return_type": "System.Void",
                        "is_static": True,
                        "parameters": [],
                    }
                ],
                "properties": [
                    {
                        "name": "Id",
                        "kind": "property",
                        "return_type": "System.String",
                        "is_static": False,
                        "parameters": [],
                    }
                ],
                "fields": [
                    {
                        "name": "_id",
                        "kind": "field",
                        "return_type": "System.String",
                        "is_static": False,
                        "parameters": [],
                    }
                ],
            }
        )

        self.assertEqual(reflected.type_name, "MegaCrit.Sts2.Core.Modding.ModManifest")
        self.assertEqual(reflected.constructors[0].parameters[0].type_name, "System.String")
        self.assertEqual(reflected.methods[0].name, "Load")
        self.assertEqual(reflected.properties[0].return_type, "System.String")
        self.assertEqual(reflected.fields[0].name, "_id")

    def test_type_listing_result_from_payload_parses_types(self) -> None:
        listing = TypeListingResult.from_payload(
            {
                "assembly_path": "C:\\game\\sts2.dll",
                "namespace_prefix": "MegaCrit.Sts2.Core.Models.Cards",
                "assignable_to": "MegaCrit.Sts2.Core.Models.CardModel",
                "types": [
                    {
                        "type_name": "MegaCrit.Sts2.Core.Models.Cards.Whirlwind",
                        "base_type": "MegaCrit.Sts2.Core.Models.CardModel",
                        "is_abstract": False,
                        "has_parameterless_constructor": True,
                    }
                ],
            }
        )

        self.assertEqual(listing.namespace_prefix, "MegaCrit.Sts2.Core.Models.Cards")
        self.assertEqual(listing.types[0].type_name, "MegaCrit.Sts2.Core.Models.Cards.Whirlwind")
        self.assertTrue(listing.types[0].has_parameterless_constructor)


if __name__ == "__main__":
    unittest.main()
