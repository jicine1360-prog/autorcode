"""geo(생활 정보) 도구 테스트 — 네트워크 없이 _get 을 스텁으로 갈아끼운다."""
import urllib.error
import unittest

from harness import geo


def _fake(responses):
    calls = []

    def stub(url, timeout=30):
        calls.append(url)
        for match, body in responses:
            if match in url:
                return body
        raise AssertionError(f"no fixture for: {url[:120]}")

    geo._get = stub
    return calls


class GeoTest(unittest.TestCase):
    def test_geocode_parses_nominatim(self):
        _fake([("/search?", [{"lat": "37.556", "lon": "126.972",
                              "display_name": "서울역, ..."}]),
               ])
        got = geo.geocode("서울역")
        self.assertEqual(got["lat"], 37.556)
        self.assertEqual(got["name"], "서울역, ...")

    def test_geocode_empty(self):
        _fake([("/search?", [])])
        self.assertIsNone(geo.geocode("없는곳"))

    def test_nearby_formats_sorted(self):
        elements = [
            {"type": "node", "id": 1, "lat": 37.556, "lon": 126.972,
             "tags": {"name": "카페A", "addr:street": "세종대로"}},
            {"type": "way", "id": 2, "center": {"lat": 37.550, "lon": 126.978},
             "tags": {"name": "카페B소식구멍다"}},
        ]
        calls = _fake([("/api/interpreter?", {"elements": elements})])
        out = geo.nearby("카페", 37.556, 126.972, radius=2000, limit=8)
        self.assertIn("카페", out)
        self.assertIn("카페A", out)
        self.assertLess(out.index("카페A"), out.index("카페B"))  # 거리순

    def test_nearby_unknown_kind(self):
        out = geo.nearby("우주선", 1.0, 1.0)
        self.assertIn("지원 종류", out)

    def test_route_formats_osrm(self):
        body = {"routes": [{
            "distance": 24300.0, "duration": 1500,
            "legs": [{"steps": [
                {"name": "", "maneuver": {"instruction": "Start heading north"}},
                {"name": "", "maneuver": {"instruction": "Turn right"}},
            ]}],
        }]}
        calls = _fake([("/route/v1/driving", body)])
        out = geo.route("37.556,126.972", "37.55,126.97")
        self.assertIn("24.3 km", out)
        self.assertIn("25분", out)
        self.assertIn("Turn right", out)
        # 두 지점 모두 좌표면 별도 지오코딩 없이 OSRM 호출
        self.assertEqual(len([c for c in calls if "maps.org" in c]), 0)

    def test_nearby_rotates_overpass_mirrors(self):
        calls = []

        def stub(url, timeout=30):
            calls.append(url)
            if "overpass-api.de" in url:
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            return {"elements": [{"type": "node", "lat": 37.5, "lon": 126.9,
                                  "tags": {"name": "미러카페"}}]}

        geo._get = stub
        out = geo.nearby("카페", 37.55, 126.97, radius=1000)
        self.assertIn("미러카페", out)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn(".kumi.systems", calls[1])

    def test_nearby_query_is_single_union(self):
        seen = {}

        def stub(url, timeout=30):
            seen["q"] = urllib.parse.unquote(url).split("?data=")[1]
            return {"elements": []}

        geo._get = stub
        geo.nearby("카페", 37.55, 126.97)
        q = seen["q"]
        self.assertIn("(node[", q)
        self.assertNotIn("((n", q)  # 이중 괄호는 Overpass 500 유발 (회귀 방지)

    def test_nearby_regex_merges_same_key(self):
        # 맛집(amenity 3종)은 같은 키 정규식 1쌍으로 병합 → 질의 1개만
        clauses = list(geo._tag_clauses({"amenity": ["restaurant", "cafe"]}))
        self.assertEqual(len(clauses), 1)
        self.assertIn("^(", clauses[0])


if __name__ == "__main__":
    unittest.main()