from __future__ import annotations

import unittest

from benchmark.definitions.partitions import Shard


class TestToToken(unittest.TestCase):
    """A per-identifier partition key: 8 hex chars, collision-resistant."""

    def test_width_is_eight_hex(self) -> None:
        token = Shard.to_token("microsoft/Olive#646")
        self.assertEqual(len(token), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in token))

    def test_deterministic(self) -> None:
        self.assertEqual(
            Shard.to_token("awslabs/graphstorm#323"),
            Shard.to_token("awslabs/graphstorm#323"),
        )

    def test_distinct_identifiers_distinct_tokens(self) -> None:
        self.assertNotEqual(
            Shard.to_token("microsoft/Olive#646"),
            Shard.to_token("microsoft/Olive#1206"),
        )

    def test_resolves_the_four_hex_collisions(self) -> None:
        # The pairs that collided under the old 4-hex space must now differ.
        collisions = [
            ("microsoft/Olive#646", "microsoft/Olive#1206"),
            ("mistralai/mistral-common#175", "awslabs/graphstorm#323"),
            ("awslabs/graphstorm#926", "microsoft/Olive#1626"),
        ]
        for left, right in collisions:
            self.assertEqual(Shard.to_space([left]), Shard.to_space([right]))
            self.assertNotEqual(Shard.to_token(left), Shard.to_token(right))


class TestToSpaceUnchanged(unittest.TestCase):
    """to_space keeps its 4-hex space width so shard names are untouched."""

    def test_space_is_four_hex(self) -> None:
        self.assertEqual(len(Shard.to_space(["a", "b", "c"])), 4)

    def test_element_encoding_unchanged(self) -> None:
        names = ["a", "b", "c"]
        token = Shard.to_space(names, "b")
        # 4-hex space plus the index suffix, decodable back to the element.
        self.assertTrue(token.startswith(Shard.to_space(names)))
        self.assertEqual(Shard.from_space(token, names), "b")


if __name__ == "__main__":
    unittest.main()
