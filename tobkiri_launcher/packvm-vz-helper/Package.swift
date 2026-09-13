// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TobkiriPackVMVZHelper",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "PackVMVZCore", targets: ["PackVMVZCore"]),
        .executable(name: "tobkiri-packvm-vz-helper", targets: ["PackVMVZHelper"]),
    ],
    targets: [
        .target(
            name: "PackVMVZCore",
            linkerSettings: [.linkedFramework("Virtualization"), .linkedFramework("Security")]
        ),
        .executableTarget(
            name: "PackVMVZHelper",
            dependencies: ["PackVMVZCore"]
        ),
        .testTarget(
            name: "PackVMVZCoreTests",
            dependencies: ["PackVMVZCore"]
        ),
    ]
)
