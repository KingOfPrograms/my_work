"""
Excel 勾选统计工具
==================
在 Web 页面勾选数据行 → 选择统计维度 → 下载含统计结果的新 Excel。
启动: streamlit run excel_stats_app.py
"""

import io
import streamlit as st
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

st.set_page_config(page_title="Excel 数据统计工具", layout="wide")
st.title("Excel 数据统计工具")

# ---------------------------------------------------------------------------
# 1. 加载数据
# ---------------------------------------------------------------------------
DEFAULT_FILE = "test_data_template.xlsx"

uploaded = st.file_uploader("上传 Excel 文件（留空则使用默认文件）", type=["xlsx", "xls"])
source = uploaded if uploaded else DEFAULT_FILE

try:
    if isinstance(source, str):
        all_sheets = pd.read_excel(source, sheet_name=None)
        # 同时用 openpyxl 读取原始文件，方便后续写回
        import openpyxl as _xl

        _orig_wb = _xl.load_workbook(source)
    else:
        all_sheets = pd.read_excel(source, sheet_name=None)
        _orig_wb = None
except Exception as e:
    st.error(f"文件读取失败: {e}")
    st.stop()

sheet_names = list(all_sheets.keys())
tab_labels = sheet_names if len(sheet_names) > 1 else sheet_names
tabs = st.tabs(tab_labels)

# ---------------------------------------------------------------------------
# 2. 每页渲染带勾选的数据表
# ---------------------------------------------------------------------------
selected_indices = {}  # sheet_name → list of selected row indices
filter_state = {}  # sheet_name → {col: selected values}

for idx, sname in enumerate(sheet_names):
    df = all_sheets[sname].copy()
    with tabs[idx]:
        st.subheader(sname)

        # --- 顶部筛选栏 ---
        with st.expander("筛选条件", expanded=False):
            cols_filter = st.columns(min(4, len(df.columns)))
            for ci, col in enumerate(df.columns):
                with cols_filter[ci % 4]:
                    vals = df[col].dropna().unique().tolist()
                    if len(vals) <= 50:
                        selected = st.multiselect(
                            str(col)[:20],
                            options=vals,
                            key=f"flt_{sname}_{col}",
                        )
                        if selected:
                            df = df[df[col].isin(selected)]
                    elif col not in ("filepath",):  # 跳过长文本列
                        text_input = st.text_input(
                            str(col)[:20],
                            key=f"flt_txt_{sname}_{col}",
                        )
                        if text_input:
                            df = df[df[col].astype(str).str.contains(text_input, na=False)]

        # --- 数据表格（带勾选列） ---
        df_display = df.copy()
        df_display.insert(0, "选择", True)

        edited = st.data_editor(
            df_display,
            use_container_width=True,
            hide_index=True,
            column_config={"选择": st.column_config.CheckboxColumn(width="small")},
            num_rows="dynamic",
            key=f"editor_{sname}",
        )

        # 记录勾选的行（在原 df 中的索引）
        selected_mask = edited["选择"].values if "选择" in edited.columns else []
        selected_idx = (
            df.index[selected_mask].tolist()
            if len(selected_mask) == len(df)
            else df.index.tolist()
        )
        selected_indices[sname] = selected_idx

        st.caption(f"已选 {len(selected_idx)} / {len(df)} 条")

# ---------------------------------------------------------------------------
# 3. 统计配置
# ---------------------------------------------------------------------------
st.divider()
st.subheader("统计配置")

# 获取第一个 sheet 的列名作为统计维度选项
main_df = all_sheets[sheet_names[0]]
stat_cols = [c for c in main_df.columns if c != "filepath"]

col1, col2 = st.columns(2)
with col1:
    stat_mode = st.selectbox(
        "统计方式",
        ["分组计数", "通过率统计 (process_conclusion)", "error_tag 分布"],
        key="stat_mode",
    )
with col2:
    if "分组计数" in stat_mode:
        group_cols = st.multiselect(
            "分组列（可多选，空则默认一级分类）",
            options=stat_cols,
            default=["一级分类"] if "一级分类" in stat_cols else [],
            key="group_cols",
        )
    else:
        group_cols = []

do_extra_pass_rate = st.checkbox("同时输出通过率统计", value=True, key="extra_pass")
do_extra_error_tag = st.checkbox("同时输出 error_tag 分布", value=True, key="extra_err")

# ---------------------------------------------------------------------------
# 4. 执行统计 & 输出 Excel
# ---------------------------------------------------------------------------
if st.button("执行统计并生成 Excel", type="primary"):
    # 收集所有选中行的数据
    all_selected = []
    for sname in sheet_names:
        df = all_sheets[sname]
        idx = selected_indices.get(sname, [])
        selected = df.loc[df.index.isin(idx)].copy()
        selected["_来源Sheet"] = sname
        all_selected.append(selected)

    if not all_selected:
        st.warning("未选择任何数据")
        st.stop()

    combined = pd.concat(all_selected, ignore_index=True)
    st.info(f"参与统计的选中数据共 {len(combined)} 条")

    # --- 生成统计结果 ---
    stats_tables = {}  # title → DataFrame

    # 分组计数
    if "分组计数" in stat_mode:
        gcols = group_cols if group_cols else ["一级分类"]
        gcols_exist = [c for c in gcols if c in combined.columns]
        if gcols_exist:
            count_df = (
                combined.groupby(gcols_exist)
                .size()
                .reset_index(name="数量")
                .sort_values("数量", ascending=False)
            )
            count_df["占比"] = (count_df["数量"] / count_df["数量"].sum() * 100).round(1).astype(str) + "%"
            stats_tables["分组计数"] = count_df

    # 通过率
    if do_extra_pass_rate or "通过率" in stat_mode:
        if "process_conclusion" in combined.columns:
            pass_df = (
                combined.groupby("process_conclusion")
                .size()
                .reset_index(name="数量")
                .sort_values("数量", ascending=False)
            )
            pass_df["占比"] = (pass_df["数量"] / pass_df["数量"].sum() * 100).round(1).astype(str) + "%"
            stats_tables["通过率统计"] = pass_df

    # error_tag
    if do_extra_error_tag or "error_tag" in stat_mode:
        if "process_error_tag" in combined.columns:
            tags = combined["process_error_tag"].dropna().astype(str)
            tags = tags[tags.str.strip() != ""]
            if len(tags) > 0:
                tag_df = tags.value_counts().reset_index()
                tag_df.columns = ["process_error_tag", "数量"]
                tag_df["占比"] = (tag_df["数量"] / tag_df["数量"].sum() * 100).round(1).astype(str) + "%"
                stats_tables["error_tag分布"] = tag_df

    if not stats_tables:
        st.warning("没有可生成的统计数据")
        st.stop()

    # 展示预览
    for title, sdf in stats_tables.items():
        st.markdown(f"**{title}**")
        st.dataframe(sdf, use_container_width=True, hide_index=True)

    # --- 生成输出 Excel ---
    output = io.BytesIO()

    if _orig_wb is not None:
        wb = _orig_wb
    else:
        wb = Workbook()
        if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1:
            ws_default = wb["Sheet"]
            for ci, col_name in enumerate(main_df.columns, 1):
                ws_default.cell(row=1, column=ci, value=col_name)
            for ri, row in main_df.itertuples(index=False):
                for ci, val in enumerate(row, 1):
                    ws_default.cell(row=ri + 2, column=ci, value=val)

    # 新增/覆盖统计 sheet
    stats_sname = "统计分析"
    if stats_sname in wb.sheetnames:
        del wb[stats_sname]
    ws_stats = wb.create_sheet(stats_sname)

    header_font = Font(bold=True, size=12)
    header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    section_font = Font(bold=True, size=13, color="1F4E79")

    current_row = 1
    for title, sdf in stats_tables.items():
        # 节标题
        ws_stats.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=len(sdf.columns))
        title_cell = ws_stats.cell(row=current_row, column=1, value=title)
        title_cell.font = section_font
        current_row += 1

        # 表头
        for ci, col_name in enumerate(sdf.columns, 1):
            cell = ws_stats.cell(row=current_row, column=ci, value=str(col_name))
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        current_row += 1

        # 数据
        for _, row_data in sdf.iterrows():
            for ci, val in enumerate(row_data, 1):
                ws_stats.cell(row=current_row, column=ci, value=val)
            current_row += 1

        current_row += 2  # 空行分隔

    # 调整列宽
    for ci in range(1, 10):
        ws_stats.column_dimensions[get_column_letter(ci)].width = 22

    wb.save(output)
    output.seek(0)

    st.success("统计完成，点击下方按钮下载")
    st.download_button(
        label="下载统计 Excel",
        data=output,
        file_name="统计数据_结果.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
