"""Unit tests for area_trends.py's pure helpers (no data files, no network).

Run:  python -m unittest discover -s pipeline -p 'test_*.py'
"""
import math
import unittest

import numpy as np
import shapely
from shapely.geometry import box

import area_trends as T


class MonthsTest(unittest.TestCase):
    def test_month_index_and_labels(self):
        self.assertEqual(T.month_index("2016-01-31", 2016), 0)
        self.assertEqual(T.month_index("2017-03-01", 2016), 14)
        self.assertEqual(T.month_index("2015-12-31", 2016), -1)
        self.assertEqual(T.month_labels(2016, 14)[-1], "2017-02")


class IdsAndWardsTest(unittest.TestCase):
    def test_slug_makes_cross_ward_ids_filename_safe(self):
        self.assertEqual(T.slug("3/4G"), "3-4G")
        self.assertEqual(T.slug("3/4G01"), "3-4G01")
        self.assertEqual(T.slug("1A05"), "1A05")

    def test_ward_digit(self):
        self.assertEqual(T.ward_digit("Ward 5"), "5")
        self.assertEqual(T.ward_digit("Null"), "")
        self.assertEqual(T.ward_digit(""), "")

    def test_default_ward(self):
        self.assertEqual(T.default_ward("1A"), "1")
        self.assertIsNone(T.default_ward("3/4G"))

    def test_majority_ward_ignores_unplaced_and_blank(self):
        smd = np.array([0, 0, 0, 1, 1, -1, 1])
        wards = ["3", "4", "4", "", "", "8", "3"]
        self.assertEqual(T.majority_ward(smd, wards), {0: "4", 1: "3"})

    def test_resolve_wards_splits_a_cross_ward_anc(self):
        smds = ["1A01", "3/4G01", "3/4G02", "3/4G03"]
        anc_of = {"1A01": "1A", "3/4G01": "3/4G", "3/4G02": "3/4G", "3/4G03": "3/4G"}
        # 3/4G01 votes Ward 3, 3/4G02 votes Ward 4, 3/4G03 has no recent rows
        ward_of, odd = T.resolve_wards(smds, anc_of, {1: "3", 2: "4"})
        self.assertEqual(ward_of, {"1A01": "1", "3/4G01": "3", "3/4G02": "4", "3/4G03": "3"})
        self.assertEqual(odd, [])

    def test_resolve_wards_reports_disagreement(self):
        ward_of, odd = T.resolve_wards(["1A01"], {"1A01": "1A"}, {0: "2"})
        self.assertEqual(ward_of["1A01"], "2")
        self.assertEqual(odd, [("1A01", "1", "2")])

    def test_area_tree_hierarchy(self):
        smds = ["1A01", "1A02", "3/4G01", "3/4G02", "3/4G03"]
        anc_of = {"1A01": "1A", "1A02": "1A", "3/4G01": "3/4G", "3/4G02": "3/4G", "3/4G03": "3/4G"}
        ward_of = {"1A01": "1", "1A02": "1", "3/4G01": "3", "3/4G02": "4", "3/4G03": "4"}
        areas, wards, ancs, smd_anc, smd_ward = T.area_tree(smds, anc_of, ward_of)
        by = {a["id"]: a for a in areas}
        self.assertEqual(wards, ["1", "3", "4"])
        self.assertEqual(by["city"]["n_smd"], 5)
        self.assertEqual(by["ward-4"]["n_smd"], 2)
        g = by["anc-3-4G"]
        self.assertEqual(g["wards"], ["ward-3", "ward-4"])
        self.assertEqual(g["ward"], "ward-4")          # holds 2 of its 3 SMDs
        self.assertEqual(by["smd-3-4G01"]["ward"], "ward-3")  # its own ward, not the ANC's
        self.assertEqual(by["smd-3-4G01"]["anc"], "anc-3-4G")
        self.assertEqual(smd_ward.tolist(), [0, 0, 1, 2, 2])
        self.assertEqual(smd_anc.tolist(), [0, 0, 1, 1, 1])
        self.assertTrue(all("/" not in a["id"] for a in areas))


class CategoriesTest(unittest.TestCase):
    def test_top_n_plus_presets(self):
        totals = {"A": 100, "B": 50, "C": 10, "D": 5, "Z": 0}
        presets = [{"key": "p", "label": "p", "codes": ["D", "Z", "MISSING"]}]
        self.assertEqual(T.pick_categories(totals, presets, 2), ["A", "B", "D"])


class CubeTest(unittest.TestCase):
    def test_count_cube(self):
        area = np.array([0, 0, 1, 1, 1])
        cat = np.array([0, 1, 1, 1, 0])
        mon = np.array([0, 2, 2, 2, 1])
        c = T.count_cube(area, cat, mon, 2, 2, 3)
        self.assertEqual(c.shape, (2, 2, 3))
        self.assertEqual(c[1, 1, 2], 2)
        self.assertEqual(c[0, 1, 2], 1)
        self.assertEqual(int(c.sum()), 5)

    def test_median_cube_min_n_even_and_open(self):
        O = T.OPEN_DAYS
        # cell (0,0,0): days 1,3,5,7 -> median 4 ; cell (0,0,1): two rows (< min_n=3)
        # cell (0,0,2): 2 closed + 2 open -> median sits on an open one -> NaN
        # cell (0,0,3): 3 closed + 1 open -> sorted [2,4,6,O] -> median 5
        area = np.zeros(13, dtype=int)
        cat = np.zeros(13, dtype=int)
        mon = np.array([0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3])
        days = np.array([7, 1, 5, 3, 1, 2, 1, 2, O, O, 6, 2, 4])
        area, cat = np.append(area, 0), np.append(cat, 0)
        mon, days = np.append(mon, 3), np.append(days, O)
        m = T.median_cube(area, cat, mon, days, 1, 1, 4, min_n=3)[0, 0]
        self.assertEqual(m[0], 4)
        self.assertTrue(math.isnan(m[1]))
        self.assertTrue(math.isnan(m[2]))
        self.assertEqual(m[3], 5)

    def test_series_rounding_and_no_medians(self):
        c, m = T.series(np.array([1, 2]), np.array([np.nan, 2.5]))
        self.assertEqual((c, m), ([1, 2], [None, 2.5]))
        self.assertEqual(T.series(np.array([3]))[1], None)


class AssignTest(unittest.TestCase):
    def test_assign_points(self):
        tree = shapely.STRtree([box(0, 0, 1, 1), box(1, 0, 2, 1)])
        lats = np.array([0.5, 0.5, 5.0, np.nan])
        lons = np.array([0.5, 1.5, 5.0, np.nan])
        self.assertEqual(T.assign_points(lats, lons, tree).tolist(), [0, 1, -1, -1])


if __name__ == "__main__":
    unittest.main()
