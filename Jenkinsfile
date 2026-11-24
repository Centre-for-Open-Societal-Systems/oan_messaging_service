// Build the OAN Messaging service image and publish it to AWS ECR:
//   379220350808.dkr.ecr.ap-south-1.amazonaws.com/eoan/<repo name>/<service>:<version>
// <version> = the pyproject version plus the build number (for example 0.1.0-develop.7), the same style as the
// other eoan/ images; a second tag carries the 12-character commit. Tags in the repository are immutable.
// Jenkins job: oan-messaging-service (ci/jenkins/jobs/oan-messaging-service.xml), builds the develop branch; develop publishes.
// Needs the Jenkins credential aws-ecr-creds-new (AWS Credentials with ECR push access).
pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        AWS_REGION     = 'ap-south-1'
        IMAGE_REGISTRY = '379220350808.dkr.ecr.ap-south-1.amazonaws.com'
        SERVICE        = 'oan-messaging'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    def base = sh(returnStdout: true, script: "grep -m1 '^version' pyproject.toml | cut -d'\"' -f2").trim()
                    def repo = sh(returnStdout: true, script: 'basename -s .git "$(git config --get remote.origin.url)"').trim()
                    def sha  = sh(returnStdout: true, script: 'git rev-parse --short=12 HEAD').trim()
                    env.IMAGE_VERSION = "${base}-develop.${env.BUILD_NUMBER}"
                    env.ECR_REPO      = "eoan/${repo}/${env.SERVICE}"
                    env.IMAGE         = "${env.IMAGE_REGISTRY}/${env.ECR_REPO}"
                    env.IMAGE_SHA     = sha
                    echo "Image ${env.IMAGE}:${env.IMAGE_VERSION} (commit ${sha})"
                }
            }
        }

        stage('Build image') {
            steps {
                sh '''
                    docker build -t "$IMAGE:$IMAGE_VERSION" -t "$IMAGE:$IMAGE_SHA" .
                '''
            }
        }

        stage('Push image') {
            when { branch 'develop' }
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds-new']]) {
                    sh '''
                        aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$ECR_REPO" >/dev/null 2>&1 || \
                            aws ecr create-repository --region "$AWS_REGION" --repository-name "$ECR_REPO" \
                                --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true
                        aws ecr get-login-password --region "$AWS_REGION" \
                            | docker login --username AWS --password-stdin "$IMAGE_REGISTRY"
                        docker push "$IMAGE:$IMAGE_VERSION"
                        docker push "$IMAGE:$IMAGE_SHA"
                    '''
                }
            }
        }
    }

    post {
        success { echo "Published ${env.IMAGE}:${env.IMAGE_VERSION}" }
        always  { sh 'docker rmi "$IMAGE:$IMAGE_VERSION" "$IMAGE:$IMAGE_SHA" >/dev/null 2>&1 || true' }
    }
}
