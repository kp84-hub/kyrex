from setuptools import setup, find_packages

setup(
    name="kyrex-engine",
    version="0.1.0",
    description="Kyrex — minimalist terminal agent toolkit",
    packages=find_packages(),
    package_data={"kyrex": ["assets/*.json"]},
    install_requires=[
        "openai>=1.0.0",
        "anthropic>=0.30.0",
        "requests>=2.28.0",
        "textual>=1.0.0",
    ],
    extras_require={
        "mcp": ["mcp>=1.0.0"],
    },
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "kyrex=kyrex.cli:main",
        ],
    },
)
