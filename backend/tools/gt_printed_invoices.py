"""步骤 07.0b：6 张印刷体供应商发票的 Ground Truth（开发方标注）。

标注方式：逐张打开 `samples/real/pages/` 下的原图，按格子抄写原文，不看任何模型
输出。这一条很重要——GT 若是照着模型输出校对出来的，模型错在哪它就跟着错在哪，
评测会得出「准确率很高」的假结论。

**这 6 张是全打印件**（Kun Kee 1 张、New Moy's 5 张），纸面无手写内容（New Moy's
的签名栏除外，不属于任何字段）。68 张中文手写复写纸**不在这里**：步骤 07 已确认
连母语者都认不出那些连笔速记，手写件不要求 GT，改走虚报率 / 分歧率 / 算术不自洽率
三个客观指标。

**算术自洽是标注完之后的体检，不是标注的依据。** 先抄完，再用 check_arithmetic()
回头验；如果某一行算不平，要回原图看是抄错了还是单据本身就不平，不能倒过来按
算得平去改数字——那正是步骤 07 在模型身上实测到的「自洽的编造」。

生成：python tools/gt_printed_invoices.py
"""

from __future__ import annotations

from pathlib import Path

from gt_schema import GroundTruth, build_document, build_line, check_arithmetic

ANNOTATOR = "dev（逐张读原图，未参考模型输出）"
ANNOTATED_ON = "2026-11-10"

OUT_DIR = Path(__file__).resolve().parent.parent / "samples" / "real" / "ground_truth"


def kun_kee_p01() -> GroundTruth:
    """03_Kunkee_Trading_1p__p01.jpg

    版式：Item code / Qty / Description / price Each / Amount 五列。
    包装规格 `1000g x 10` 混在 Description 里，没有独立列——这正是 pack_spec
    必须单列一栏的理由（步骤 09 的换算要用，从描述文本里再抠一次会很脆）。
    `Qty` 列的 20 是箱数，单位「箱」写在描述开头，所以 unit 记「箱」。
    """
    return GroundTruth(
        image="03_Kunkee_Trading_1p__p01.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="KUN KEE TRADING INC.",
            invoice_date="06/26/2026",
            invoice_number="180311",
            invoice_total="$1,040.00",
            currency="无",  # 纸面没有写币种，只有 $ 符号
        ),
        lines=[
            build_line(
                1,
                item_description="箱　麻薯粉 WHEAT FLOUR 1000g x 10",
                quantity="20",
                unit="箱",
                unit_price="52.00",
                line_amount="1,040.00",
                pack_spec="1000g x 10",
                note="Item code W18121；包装规格写在 Description 里，无独立列",
            )
        ],
        notes=[
            "整页为打印件；右下角有手写签名，不属于任何字段，不计分。",
            "纸张为浅黄色，扫描后底色偏黄；表格下方大片空白格。",
            "Total 行印着 $1,040.00，与唯一一行的 Amount 相同。",
        ],
    )


def new_moys_p01() -> GroundTruth:
    """04_NewMoys_Meat_5p__p01.jpg — Invoice # 93"""
    return GroundTruth(
        image="04_NewMoys_Meat_5p__p01.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="NEW MOY'S MEAT MARKET INC.",
            invoice_date="6/3/2026",
            invoice_number="93",
            invoice_total="$885.00",
            currency="无",
        ),
        lines=[
            build_line(
                1,
                item_description="Pork Spare Rib(Cube) - 靓排粒",
                quantity="180",
                unit="lbs.",
                unit_price="$ 2.99",
                line_amount="$538.20",
                pack_spec="6X30",
            ),
            build_line(
                2,
                item_description="Beef Tripe - 牛柏叶",
                quantity="120",
                unit="lbs.",
                unit_price="$ 2.89",
                line_amount="$346.80",
                pack_spec="4X30",
            ),
        ],
        notes=["Term 30 days；Cases 列即包装规格，记入 pack_spec。"],
    )


def new_moys_p02() -> GroundTruth:
    """04_NewMoys_Meat_5p__p02.jpg — Invoice # 97

    第 1 行的 Cases 是裸数字 `1`（不是 `nXm` 形式），说明这家的 Cases 列有时是
    箱数、有时是包装规格。步骤 09 不能假设这一列永远可解析成「每箱几磅」。
    """
    return GroundTruth(
        image="04_NewMoys_Meat_5p__p02.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="NEW MOY'S MEAT MARKET INC.",
            invoice_date="6/6/2026",
            invoice_number="97",
            invoice_total="$465.51",
            currency="无",
        ),
        lines=[
            build_line(
                1,
                item_description="Pork - CT",
                quantity="35.69",
                unit="lbs.",
                unit_price="$ 2.99",
                line_amount="$106.71",
                pack_spec="1",
                note="Cases 为裸数字 1，不是 nXm 形式",
            ),
            build_line(
                2,
                item_description="Pork Spare Rib(Cube) - 靓排粒",
                quantity="120",
                unit="lbs.",
                unit_price="$ 2.99",
                line_amount="$358.80",
                pack_spec="4X30",
            ),
        ],
        notes=["抬头 'NEW MOY'S MEA? MARKET' 有一处墨点遮挡，不影响字段。"],
    )


def new_moys_p03() -> GroundTruth:
    """04_NewMoys_Meat_5p__p03.jpg — Invoice # 99

    **本页是行匹配的关键测试点。** 第 1 行下面印着一行 `55.8  56.8`，它不是一条
    商品行，而是第 1 行两箱的分箱重量（55.8 + 56.8 = 112.6，正好是第 1 行的
    Quantity）。模型若把它当成独立商品行输出，就是一次多报（false row）。
    """
    return GroundTruth(
        image="04_NewMoys_Meat_5p__p03.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="NEW MOY'S MEAT MARKET INC.",
            invoice_date="6/8/2026",
            invoice_number="99",
            invoice_total="$672.21",
            currency="无",
        ),
        lines=[
            build_line(
                1,
                item_description="Pork - CT",
                quantity="112.6",
                unit="lbs.",
                unit_price="$ 2.89",
                line_amount="$325.41",
                pack_spec="2",
                note="描述格下方另印 '55.8 56.8'，是两箱分重（55.8+56.8=112.6），不是独立行",
            ),
            build_line(
                2,
                item_description="Beef Tripe - 牛柏叶",
                quantity="120",
                unit="lbs.",
                unit_price="$ 2.89",
                line_amount="$346.80",
                pack_spec="4X30",
            ),
        ],
        notes=[
            "'55.8 56.8' 是分箱重量，不是商品行；模型把它输出成一行即为多报。",
        ],
    )


def new_moys_p04() -> GroundTruth:
    """04_NewMoys_Meat_5p__p04.jpg — Invoice # 2

    第 2 行把包装规格 `6X44.10` 直接粘在中文品名后面（`牛肚6X44.10`），Cases 列
    另写 `6`。pack_spec 记描述里那个带每箱重量的写法，它对步骤 09 更有用。
    """
    return GroundTruth(
        image="04_NewMoys_Meat_5p__p04.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="NEW MOY'S MEAT MARKET INC.",
            invoice_date="6/11/2026",
            invoice_number="2",
            invoice_total="$1,900.76",
            currency="无",
        ),
        lines=[
            build_line(
                1,
                item_description="Beef Eye Round - 老牛",
                quantity="37",
                unit="lbs.",
                unit_price="$ 6.39",
                line_amount="$236.43",
                pack_spec="1",
            ),
            build_line(
                2,
                item_description="Beef Stomach - 牛肚6X44.10",
                quantity="264.6",
                unit="lbs.",
                unit_price="$ 6.29",
                line_amount="$1,664.33",
                pack_spec="6X44.10",
                note="规格粘在品名后；Cases 列只写 6。6 × 44.10 = 264.6 与 Quantity 一致",
            ),
        ],
        notes=["发票号只有一位数 '2'，与同一叠里的 93 / 97 / 99 不连号。"],
    )


def new_moys_p05() -> GroundTruth:
    """04_NewMoys_Meat_5p__p05.jpg — Invoice # 10

    五行，本批印刷件里行数最多的一张，同时含两种「规格粘在品名里」的写法
    （`牛肚6X44.10`、`肥板2X60.1`）和一处分箱重量（`54  52.7`）。
    """
    return GroundTruth(
        image="04_NewMoys_Meat_5p__p05.jpg",
        doc_class="printed",
        annotator=ANNOTATOR,
        annotated_on=ANNOTATED_ON,
        document=build_document(
            supplier="NEW MOY'S MEAT MARKET INC.",
            invoice_date="6/18/2026",
            invoice_number="10",
            invoice_total="$2,483.41",
            currency="无",
        ),
        lines=[
            build_line(
                1,
                item_description="Beef Stomach - 牛肚6X44.10",
                quantity="264.6",
                unit="lbs.",
                unit_price="$ 6.29",
                line_amount="$1,664.33",
                pack_spec="6X44.10",
            ),
            build_line(
                2,
                item_description="Pork - CT",
                quantity="106.7",
                unit="lbs.",
                unit_price="$ 2.69",
                line_amount="$287.02",
                pack_spec="2",
                note="描述格下方另印 '54 52.7'，是两箱分重（54+52.7=106.7），不是独立行",
            ),
            build_line(
                3,
                item_description="Pork Backfat - 肥板2X60.1",
                quantity="120.2",
                unit="lbs.",
                unit_price="$ 1.49",
                line_amount="$179.10",
                pack_spec="2X60.1",
            ),
            build_line(
                4,
                item_description="Beef Eye Round - 老牛",
                quantity="29.50",
                unit="lbs.",
                unit_price="$ 6.29",
                line_amount="$185.56",
                pack_spec="1",
            ),
            build_line(
                5,
                item_description="Duck Feet - 鸭脚",
                quantity="60",
                unit="lbs.",
                unit_price="$ 2.79",
                line_amount="$167.40",
                pack_spec="2X30",
            ),
        ],
        notes=["'54 52.7' 是分箱重量，不是商品行。"],
    )


BUILDERS = (
    kun_kee_p01,
    new_moys_p01,
    new_moys_p02,
    new_moys_p03,
    new_moys_p04,
    new_moys_p05,
)


def main() -> int:
    problems = 0
    for build in BUILDERS:
        gt = build()
        target = OUT_DIR / (Path(gt.image).stem + ".json")
        gt.save(target)
        issues = check_arithmetic(gt.to_json())
        status = "算术自洽" if not issues else f"算术提示 {len(issues)} 条"
        print(f"  {gt.image}  {len(gt.lines)} 行  {status}")
        for issue in issues:
            problems += 1
            print(f"      ! {issue}")
    print(f"\n已写入 {OUT_DIR}")
    if problems:
        print(f"共 {problems} 条算术提示——回原图确认是抄错还是单据本身不平。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
