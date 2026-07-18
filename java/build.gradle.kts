import com.vanniktech.maven.publish.JavaLibrary
import com.vanniktech.maven.publish.JavadocJar
import com.vanniktech.maven.publish.SourcesJar
import org.gradle.api.tasks.compile.JavaCompile
import org.gradle.api.tasks.javadoc.Javadoc
import org.gradle.jvm.tasks.Jar

plugins {
    `java-library`
    id("com.vanniktech.maven.publish.base") version "0.37.0"
}

group = "io.github.skyvern-ai"
version = "0.1.1"

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
    options.release = 23
}

tasks.withType<Javadoc>().configureEach {
    options.encoding = "UTF-8"
}

tasks.withType<Jar>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    if (name == "sourcesJar" || name == "plainSourcesJar") {
        exclude("native/**")
    }
}

val contractSelfTest = tasks.register<JavaExec>("contractSelfTest") {
    group = LifecycleBasePlugin.VERIFICATION_GROUP
    description = "Runs the dependency-free Java binding contract self-test."
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass = "com.skyvern.rustwright.ContractSelfTest"
}

tasks.check {
    dependsOn(contractSelfTest)
}

tasks.test {
    // ContractSelfTest is a dependency-free main class, not a JUnit test.
    failOnNoDiscoveredTests = false
}

mavenPublishing {
    configure(JavaLibrary(
        javadocJar = JavadocJar.Javadoc(),
        sourcesJar = SourcesJar.Sources(),
    ))
    coordinates("io.github.skyvern-ai", "rustwright", "0.1.1")
    publishToMavenCentral(automaticRelease = true)

    // Maven-local builds intentionally stay unsigned. The guarded release job
    // supplies this property and therefore signs every Central artifact.
    if (providers.gradleProperty("signingInMemoryKey").isPresent) {
        signAllPublications()
    }

    pom {
        name = "Rustwright Java"
        description = "Java bindings for Rustwright, a Rust CDP browser automation engine."
        url = "https://github.com/Skyvern-AI/rustwright"
        licenses {
            license {
                name = "MIT License"
                url = "https://opensource.org/licenses/MIT"
                distribution = "repo"
            }
        }
        developers {
            developer {
                id = "skyvern-ai"
                name = "Skyvern AI"
                url = "https://github.com/Skyvern-AI"
            }
        }
        scm {
            url = "https://github.com/Skyvern-AI/rustwright"
            connection = "scm:git:https://github.com/Skyvern-AI/rustwright.git"
            developerConnection = "scm:git:ssh://git@github.com/Skyvern-AI/rustwright.git"
        }
    }
}

tasks.named("assemble") {
    dependsOn("plainJavadocJar", "sourcesJar")
}
