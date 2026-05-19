import plotly.graph_objects as go
import plotly.io as pio


thesis = go.layout.Template(
    layout=go.Layout(
        font=dict(
            family="TeX Gyre Heros, Helvetica, Arial, sans-serif",
            size=11,
            color="#1A1A1A",
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        bargap=0.02,
        colorway=[
            "#1A1A1A",
            "#808080",
            "#BFBFBF",
            "#404040",
            "#666666",
        ],
        colorscale=dict(
            sequential="Viridis",
            diverging="RdBu",
        ),
        xaxis=dict(
            title=dict(standoff=20, font=dict(size=12)),
            showline=False,
            zeroline=False,
            showgrid=True,
            gridcolor="#E8E8E8",
            gridwidth=0.5,
            automargin=True,
        ),
        yaxis=dict(
            title=dict(standoff=20, font=dict(size=12)),
            showline=False,
            zeroline=False,
            showgrid=True,
            gridcolor="#E8E8E8",
            gridwidth=0.5,
        ),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.16,
            xanchor="right",
            x=1.03,
            font=dict(size=12),
        ),
        margin=dict(l=80, r=30, t=2, b=2, pad=10),
    ),
    data=dict(
        scatter=[
            go.Scatter(
                line=dict(
                    width=1,
                )
            )
        ]
    ),
)

pio.templates["thesis"] = thesis
pio.templates.default = "thesis"
