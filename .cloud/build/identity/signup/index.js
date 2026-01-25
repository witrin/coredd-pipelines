export async function handler(request, response) {
    try {
        const [, payload] = request.body.data.jwt.split(".");

        const decoded = JSON.parse(Buffer.from(payload, "base64").toString("utf8"));
        const event = decoded?.data ?? decoded ?? {};

        if (event.sign_in_method !== "github.com")
            return response
                .status(401)
                .json({ error: { message: "Invalid method" } });

        const token = process.env.GITHUB_GUARD_TOKEN;

        if (!token)
            return response
                .status(500)
                .json({ error: { message: "Missing GitHub guard token" } });

        const login = JSON.parse(event.raw_user_info).login;

        if (!login)
            return response
                .status(401)
                .json({ error: { message: "Missing GitHub username" } });

        const organization = process.env.GITHUB_ORG;

        const membership = await fetch(
            `https://api.github.com/orgs/${organization}/memberships/${login}`,
            {
                headers: {
                    Authorization: `Bearer ${token}`,
                    Accept: "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            }
        );

        if (membership.status === 200) {
            const json = await membership.json();
            if (json?.state === "active") return response.status(200).json({});
        }

        return response
            .status(401)
            .json({
                error: { message: `Organization ${organization} membership required` },
            });
    } catch (error) {
         console.log(error)
        return response.status(500).json({ error: { message: "Internal" } });
    }
}
