from datetime import datetime
import os
import sys
import fnmatch
import time
import logging
import binascii
from hashlib import md5
from glob import glob
from multiprocessing import Pool
import boto
from boto.exception import S3ResponseError
import boto.s3.connection
from boto.s3.bucket import Bucket
from boto.s3.key import Key
from math import ceil
from cStringIO import StringIO
import re

logger = logging.getLogger("radula")
logger.setLevel(logging.INFO)


class RadulaError(Exception):
    pass


class RadulaClient(object):
    DEFAULT_UPLOAD_THREADS = 10

    def __init__(self, connection=None):
        self.conn = connection
        self.profile = None
        self.thread_count = RadulaClient.DEFAULT_UPLOAD_THREADS
        self._is_secure_placeholder = None

    def connect(self, profile=None, connection=None):
        """create or reuse a boto s3 connection.
        An option aws profile may be given.
        """
        if profile:
            self.profile = profile

        if boto.config.getint('Boto', 'debug', 0) > 0:
            boto.set_stream_logger('boto')

        if connection:
            self.conn = connection

        if not self.conn:
            self.conn = self.new_connection(profile)
        return self.conn

    def new_connection(self, profile=None, **kwargs):
        """create a fresh boto s3 connection"""
        args = {}

        if profile:
            """ when directed to use a profile, check if it stipulate host, port, and/or is_
            secure and manually add those to the args """
            args['profile_name'] = profile
            profile_name = 'profile {name}'.format(name=profile)
            if boto.config.has_section(profile_name):
                port = boto.config.get(profile_name, 'port', None)
                if port:
                    args['port'] = int(port)
                host = boto.config.get(profile_name, 'host', None)
                if host:
                    args['host'] = host
                if boto.config.has_option(profile_name, 'is_secure'):
                    args['is_secure'] = boto.config.getbool(profile_name, 'is_secure', 'True')
                    self._is_secure_placeholder = boto.config.getbool('Boto', 'is_secure', None)
                    boto.config.remove_option('Boto', 'is_secure')
        else:
            """ not using a profile, check if port is set, because boto doesnt check"""
            port = boto.config.get('s3', 'port', None)
            if port:
                args['port'] = int(port)

        conn = boto.connect_s3(calling_format=boto.s3.connection.OrdinaryCallingFormat(), **args)

        if self._is_secure_placeholder is not None:
            boto.config.set('Boto', 'is_secure', self._is_secure_placeholder)
        return conn


class Radula(RadulaClient):
    BUCKET = "bucket"
    KEY = "key"

    CANNED_ACLS = (
        'private',
        'public-read',
        'public-read-write',
        'authenticated-read',
        'bucket-owner-read',
        'bucket-owner-full-control',
    )

    @staticmethod
    def split_key(subject):
        """separate a target string into bucket and key parts,
        and require that key name not be empty"""
        return Radula.split_bucket(subject, require_key=True)

    @staticmethod
    def split_bucket(subject, require_key=False):
        """separate the bucket and key components from a string,
        such as mybucket/path/to/key. boto objects may also be
        given, and they already know their constituent pieces.
        """
        if isinstance(subject, Bucket):
            return subject, None
        if isinstance(subject, Key):
            return subject.bucket, subject

        s = subject.split('/', 1)
        if len(s) == 1:
            s.append(None)

        bucket_name, key_name = tuple(s)
        if require_key and not key_name:
            raise RadulaError("Invalid target, '{0}', contains no key name".format(subject))

        return bucket_name, key_name

    def get_acl(self, **kwargs):
        """fetch a description of the ACL policy
        on a subject bucket or key"""
        subject = kwargs.get("subject", None)
        if not subject:
            raise RadulaError("Subject (bucket/key) is needed")

        try:
            bucket_name, key_name = self.split_bucket(subject)
            bucket = self.conn.get_bucket(bucket_name)

            if key_name:
                subject_type = Radula.KEY
                subject_name = key_name
                subject = bucket.get_key(key_name)
            else:
                subject = bucket
                subject_name = bucket_name
                subject_type = Radula.BUCKET

            if not subject:
                msg = "Subject {0} '{1}' not found."
                raise RadulaError(msg.format(subject_type, subject_name))
            policy = subject.get_acl()
            grants = self.format_policy(policy)
        except S3ResponseError as e:
            raise RadulaError(e.message)

        print "ACL for {0}: {1}".format(subject_type, subject.name)
        print grants

    def set_acl(self, **kwargs):
        """set the ACL policy
        on a subject bucket or key"""
        subject = kwargs.get("subject", None)
        if not subject:
            raise RadulaError("Subject (bucket/key) is needed")
        target = kwargs.get("target", None)
        if not target or target not in Radula.CANNED_ACLS:
            msg = "A canned ACL string is expected here. One of [{0}]"
            msg = msg.format(", ".join(Radula.CANNED_ACLS))
            raise RadulaError(msg)

        try:
            bucket_name, key_name = self.split_bucket(subject)
            bucket = self.conn.get_bucket(bucket_name)
            sync_acl = False

            if key_name:
                subject_type = Radula.KEY
                subject_name = key_name
                subject = bucket.get_key(key_name)
            else:
                subject = bucket
                subject_name = bucket_name
                subject_type = Radula.BUCKET
                sync_acl = True

            if not subject:
                msg = "Subject {0} '{1}' not found."
                raise RadulaError(msg.format(subject_type, subject_name))

            subject.set_acl(target)
            policy = subject.get_acl()
            grants = self.format_policy(policy)
        except S3ResponseError as e:
            raise RadulaError(e.message)

        print "ACL for {0}: {1}".format(subject_type, subject.name)
        print grants

        if sync_acl:
            self.sync_acl(subject=bucket)

    @staticmethod
    def format_policy(policy):
        """format a ACL object to a readable string"""
        grants = []
        for g in policy.acl.grants:
            grant_type = g.type
            if g.id == policy.owner.id:
                grant_type = 'CanonicalUser:OWNER'
            if g.type == 'CanonicalUser':
                u = g.display_name
            elif g.type == 'Group':
                u = g.uri
            else:
                grant_type = "email_address"
                u = g.email_address
            grants.append("[{0}] {1} = {2}".format(grant_type, u, g.permission))

        return "\n".join(sorted(grants))

    def compare_acl(self, **kwargs):
        """
        Compares bucket ACL to those of its keys, with a small report
        listing divergent policies
        """
        subject = kwargs.get("subject", None)
        if not subject:
            raise RadulaError("Subject (bucket/key) is needed")

        bucket, key = self.split_bucket(subject)
        bucket = self.conn.get_bucket(bucket)

        if key:
            keys = [bucket.get_key(key)]
        else:
            keys = bucket.list()

        bucket_acl = self.format_policy(bucket.get_acl())
        same = 0
        different = 0
        print "Bucket ACL for: {0}".format(bucket.name)
        print bucket_acl
        print "---------\n"

        for key in keys:
            key_acl = self.format_policy(key.get_acl())
            if key_acl == bucket_acl:
                same += 1
            else:
                different += 1
                print "Difference in {0}:".format(key.name)
                print key_acl
                print ""

        print "Keys with identical ACL: {0}".format(same)
        print "Keys with different ACL: {0}".format(different)

    def sync_acl(self, **kwargs):
        """
        Forces all keys in a bucket to adopt the bucket's ACL policy
        """
        subject = kwargs.get("subject", None)
        if not subject:
            raise RadulaError("Subject (bucket/key) is needed")

        bucket, key = self.split_bucket(subject)
        if not isinstance(bucket, (Bucket,)):
            bucket = self.conn.get_bucket(bucket)
        bucket_policy = bucket.get_acl()
        bucket_acl = self.format_policy(bucket_policy)

        print "Bucket ACL for: {0}".format(bucket.name)
        print bucket_acl
        print "---------\n"

        if key:
            keys = [bucket.get_key(key)]
        else:
            keys = bucket.list()

        for key in keys:
            key_policy = key.get_acl()
            if key_policy and bucket_policy and key_policy.owner.id != bucket_policy.owner.id:
                key = self.chown(key)

            print "Setting bucket's ACL on {0}".format(key.name)
            key.set_acl(bucket_policy)

    def chown(self, key):
        logger.warn("Changing ownership of key {0}".format(key.name))
        lib = RadulaLib(connection=self.conn)
        key_string = os.path.join(key.bucket.name, key.name)
        return lib.streaming_copy(key_string, key_string, force=True, verify=True)

    def allow(self, **kwargs):
        """alias of allow_user"""
        self.allow_user(**kwargs)

    def allow_user(self, **kwargs):
        """
        Grants a read or write permission to a user for a bucket or key.
        One or both of read or write are required to be truthy.
        This will guard against multiple entries for the same grants, which is
        prevalent on ceph-radosgw as well as aws-s3.
        Bucket action is recursive on keys unless apply_to_keys=False
        """
        user = kwargs.get("subject", None)
        target = kwargs.get("target", None)
        if not user:
            raise RadulaError("User is required during permission grants")
        if not target:
            raise RadulaError("A bucket or key is required during permission grants")

        read = kwargs.get("acl_read", None)
        write = kwargs.get("acl_write", None)
        if not any([read, write]):
            read = True

        bucket, key = self.split_bucket(target)
        target = self.conn.get_bucket(bucket)
        target_type = Radula.BUCKET

        if key:
            if isinstance(key, Key):
                target = key
            else:
                target = target.get_key(key)
            target_type = Radula.KEY

        permissions = []
        if read:
            permissions.append("READ")
            permissions.append("READ_ACP")
        if write:
            permissions.append("WRITE")
            permissions.append("WRITE_ACP")

        policy = target.get_acl()
        acl = policy.acl

        for permission in permissions:
            if self.is_granted(acl.grants, user, permission):
                print "User {0} already has {1} for {2} {3}, skipping".format(
                    user, permission, target_type, target.name
                )
                continue
            print "granting {0} to {1} on {2} {3}".format(permission, user, target_type, target.name)
            acl.add_user_grant(permission, user)

        target.set_acl(policy)

        if target_type == Radula.BUCKET:
            kwargs_copy = kwargs.copy()
            for key in target:
                kwargs_copy.update({"target": key})
                self.allow_user(**kwargs_copy)

    def disallow(self, **kwargs):
        """alias of disallow_user"""
        self.disallow_user(**kwargs)

    def disallow_user(self, **kwargs):
        """
        Will remove a grant from a user for a bucket or key.
        If neither read or write are specified, BOTH are assumed.
        """
        user = kwargs.get("subject", None)
        target = kwargs.get("target", None)
        if not user:
            raise RadulaError("User is required during permission grants")
        if not target:
            raise RadulaError("A bucket or key is required during permission grants")

        permissions = []
        if kwargs.get("acl_read", None):
            permissions.append("READ")
        if kwargs.get("acl_write", None):
            permissions.append("WRITE")
        if not len(permissions):
            permissions = ['READ', 'WRITE', 'READ_ACP', 'WRITE_ACP', 'FULL_CONTROL']

        bucket, key = self.split_bucket(target)
        target = self.conn.get_bucket(bucket)
        target_type = Radula.BUCKET

        if key:
            target = target.get_key(key)
            target_type = Radula.KEY
        else:
            kwargs_copy = kwargs.copy()
            for key in target:
                kwargs_copy.update({"target": key})
                self.disallow_user(**kwargs_copy)

        policy = target.get_acl()
        acl = policy.acl

        grant_count = len(acl.grants)
        acl.grants = [g for g in acl.grants if self.grant_filter(g, user, permissions, policy.owner.id, target)]
        if len(acl.grants) != grant_count:
            target.set_acl(policy)
        else:
            print "No change for {0} {1}".format(target_type, target.name)

    @staticmethod
    def is_granted(grants, user, permission):
        """
        True when the user+permission appears in the list of grants.
        """
        for grant in grants:
            if grant.id == user and grant.permission == permission:
                return True
        return False

    @staticmethod
    def grant_filter(grant, user, permissions, owner, subject):
        """
        Grant filter function, returning True for items that should be kept.
        Grants for the policy owner are kept.
        Only grants matching the user and permissions we wish to eliminate are tossed (False).
        """
        # don't restrict the owner; suspend them instead (but not here)
        if user and user == owner:
            return True
        if grant.id != user:
            return True
        if grant.permission in permissions:
            print "Grant dropped: user={0}, permission={1} on {2}".format(grant.id, grant.permission, subject)
            return False
        return True


class RadulaLib(RadulaClient):
    PROGRESS_CHUNKS = 20

    def make_bucket(self, bucket):
        """proxy make_bucket in boto"""
        for existing_bucket in self.conn.get_all_buckets():
            if bucket == existing_bucket.name:
                print "Bucket {0} already exists.".format(bucket)
                return False

        return self.conn.create_bucket(bucket)

    def remove_bucket(self, bucket):
        """proxy delete_bucket in boto"""
        return self.conn.delete_bucket(bucket)

    def get_buckets(self):
        """proxy get_all_buckets in boto"""
        return self.conn.get_all_buckets()

    def remove_key(self, subject, dry_run=False):
        """proxy delete_key in boto"""
        bucket_name, key_name = Radula.split_key(subject)
        bucket = self.conn.get_bucket(bucket_name)
        for key in self.keys(subject):
            if dry_run:
                yield 'DRY-RUN ' + key
            else:
                bucket.delete_key(key)
                yield key

    def upload(self, subject, target, verify=False):
        """initiate multipart uploads of potential plural local subject files"""
        bucket_name, target_key = Radula.split_bucket(target)
        bucket = self.conn.get_bucket(bucket_name)
        files = glob(subject)
        if len(files) == 0:
            raise RadulaError("No file(s) to upload: used {0}".format(subject))

        for source_path in files:
            key_name = guess_target_name(source_path, target_key)
            if not self._start_upload(source_path, bucket, key_name, verify):
                raise RadulaError("{0} did not correctly upload".format(source_path))

    def _start_upload(self, source, bucket, key_name, verify=False, copy_from_key=False, dest_conn=None):
        if copy_from_key:
            source_name = source
            source_size = source.size
        else:
            source_name = source
            source = open(source, 'rb')
            source_size = file_size(source)

        num_parts, chunk_size, tiny_size = calculate_chunks(source_size)
        if num_parts == 1:
            key = self._single_part_upload(
                source,
                key_name,
                bucket,
                copy_from_key=copy_from_key,
                source_size=source_size)
        else:
            key = self._multi_part_upload(
                source,
                key_name,
                bucket=bucket,
                copy_from_key=copy_from_key,
                source_size=source_size,
                num_parts=num_parts,
                chunk_size=chunk_size,
                tiny_size=tiny_size,
                dest_conn=dest_conn)

        if not verify:
            return True

        return self.verify(source_name, key, copy_from_key)

    def _single_part_upload(self, source, key_name, bucket, copy_from_key=False, source_size=0):
        t1 = time.time()
        if copy_from_key:
            data = source.get_contents_as_string()
            key = bucket.new_key(key_name)
            key.set_contents_from_string(data)
        else:
            source.seek(0)
            key = bucket.new_key(key_name)
            key.set_contents_from_file(source)
        t2 = time.time() - t1

        key = bucket.get_key(key_name)
        self.sync_acl(key, bucket)
        print_timings(key, source_size, t2)
        return key

    def _multi_part_upload(self, source, key_name, bucket,
                           copy_from_key=False, source_size=0, num_parts=0,
                           chunk_size=0, tiny_size=0, dest_conn=None):
        """multipart upload strategy.
        Borrowed heavily from the work of David Arthur
        https://github.com/mumrah/s3-multipart
        """
        if dest_conn is None:
            dest_conn = self.conn
        mpu = bucket.initiate_multipart_upload(key_name)
        logger.info("Initialized upload: %s (%s)" % (mpu.id, human_size(source_size)))

        # Generate arguments for invocations of do_part_upload
        def gen_args(num, fold_last_chunk, copy):
            for i in range(num):
                chunk_start = chunk_size * i
                if copy:
                    name = (source.bucket.name, source.name)
                else:
                    name = source.name
                args = [self.conn, bucket.name, mpu.id, name, i, chunk_start, chunk_size, num, copy, dest_conn]
                if i == (num-1) and fold_last_chunk is True:
                    args[6] = chunk_size * 2
                yield tuple(args)

        # If the last part is small, just fold it into the previous part
        fold_last = ((source_size % chunk_size) < tiny_size)

        pool = None
        try:
            # Create a pool of workers
            pool = Pool(processes=self.thread_count)
            t1 = time.time()
            pool.map_async(do_part_upload, gen_args(num_parts, fold_last, copy_from_key)).get(9999999)
            # Print out some timings
            t2 = time.time() - t1
            mpu.complete_upload()

        except KeyboardInterrupt:
            logger.warn("Received KeyboardInterrupt, cancelling upload")
            try:
                if pool:
                    pool.terminate()
                mpu.cancel_upload()
            except Exception:
                logger.error("Error while cancelling upload", exc_info=True)
                raise
            raise
        except Exception as e:
            logger.error("Encountered an error, cancelling upload", exc_info=True)
            try:
                if pool:
                    pool.terminate()
                mpu.cancel_upload()
            except Exception:
                logger.error("Error while cancelling upload", exc_info=True)
                raise e
            raise

        key = bucket.get_key(key_name)
        self.sync_acl(key, bucket)
        print_timings(key, source_size, t2)
        return key

    @staticmethod
    def sync_acl(key, bucket):
        bucket_policy = bucket.get_acl()
        bucket_owner = bucket_policy.owner.id
        key_policy = key.get_acl()
        key_owner = key_policy.owner.id

        if key_owner == bucket_owner:
            if bucket_policy.acl:
                key.set_acl(bucket_policy)
        else:
            grants = bucket_policy.acl.grants
            key_policy.acl.grants = grants
            key.set_acl(key_policy)

            for permission in ['FULL_CONTROL']:
                if not Radula.is_granted(key_policy.acl.grants, bucket_owner, permission):
                    key_policy.acl.add_user_grant(permission, bucket_owner)
                    key.set_acl(key_policy)

    def download(self, subject, target, force=False):
        """proxy download in boto, warning user about overwrites"""
        basename = os.path.basename(subject)
        if not target:
            target = basename

        if os.path.isdir(target):
            target = "/".join([target, basename])

        if os.path.isfile(target) and not force:
            msg = "File {0} exists already. Overwrite? [yN]: ".format(target)
            if not raw_input(msg).lower() == 'y':
                print "Aborting download."
                exit(0)

        bucket_name, key_name = Radula.split_key(subject)
        boto_key = self.conn.get_bucket(bucket_name).get_key(key_name)
        if not boto_key:
            raise RadulaError("Key not found: {0}".format(key_name))

        def progress_callback(a, b):
            percentage = 0
            if a:
                percentage = 100 * (float(a)/float(b))
            print "Download Progress: %.2f%%" % percentage

        boto_key.get_contents_to_filename(target, cb=progress_callback, num_cb=self.PROGRESS_CHUNKS)

    def cat(self, subject):
        """print remote file to stdout"""
        bucket_name, key_name = Radula.split_key(subject)
        boto_key = self.conn.get_bucket(bucket_name).get_key(key_name)
        if not boto_key:
            raise RadulaError("Key not found: {0}".format(key_name))

        sys.stdout.write(boto_key.get_contents_as_string())

    def keys(self, subject):
        """list keys in a bucket with consideration of glob patterns if provided"""
        bucket_name, pattern = Radula.split_bucket(subject)
        bucket = self.conn.get_bucket(bucket_name)
        if not pattern:
            for key in bucket:
                yield key.name
            return

        def _key_buffer(buffer_size=256):
            buffer_keys = []
            for k in bucket:
                buffer_keys.append(k.name)
                if len(buffer_keys) >= buffer_size:
                    filtered_keys = fnmatch.filter(buffer_keys, pattern)
                    if len(filtered_keys):
                        yield filtered_keys
                    buffer_keys = []
            yield fnmatch.filter(buffer_keys, pattern)

        for matching_keys in _key_buffer():
            for key in matching_keys:
                yield key

    def info(self, subject):
        """fetch metadata of a remote subject key"""
        bucket_name, key_name = Radula.split_bucket(subject)
        bucket = self.conn.get_bucket(bucket_name)

        if key_name:
            key = bucket.get_key(key_name)
            if not key:
                raise RadulaError("Key '{0}' not found".format(key_name))
            key.bucket = bucket_name
            return vars(key)

        total_size = 0
        object_count = 0
        largest = {"obj": None, "val": None}
        newest = {"obj": None, "val": None}
        oldest = {"obj": None, "val": None}
        for key in bucket:
            lv = largest.get("val")
            nm = newest.get("val")
            om = oldest.get("val")

            object_count += 1
            total_size += key.size
            if lv is None or key.size > lv:
                largest["obj"] = key.name
                largest["val"] = key.size

            d = datetime.strptime(key.last_modified.split(".")[0], "%Y-%m-%dT%H:%M:%S")
            if nm is None or d > nm:
                newest["obj"] = key.name
                newest["val"] = d
            if om is None or d < om:
                oldest["obj"] = key.name
                oldest["val"] = d

        return {
            "size": total_size,
            "size_human": human_size(total_size),
            "keys": {
                "count": object_count,
                "largest": largest.get("obj", None),
                "newest": newest.get("obj", None),
                "oldest": oldest.get("obj", None),
            }
        }

    def remote_md5(self, subject):
        """fetch hash from metadata of a remote subject key"""
        if type(subject) is boto.s3.key.Key:
            return subject.etag.translate(None, '"')
        else:
            bucket_name, key_name = Radula.split_key(subject)
            try:
                bucket = self.conn.get_bucket(bucket_name)
                key = bucket.get_key(key_name)
                if not key:
                    raise RadulaError("Remote file '{0}' not found".format(subject))
                return key.etag.translate(None, '"')
            except Exception as e:
                logger.info("bucket: " + bucket_name)
                logger.info("key_name: " + key_name)
                logger.error(e.message, exc_info=True)
                raise

    def local_md5(self, subject):
        """performs a multi-threaded hash of a local subject file"""
        if not os.path.isfile(subject):
            raise RadulaError("Local file '{0}' not found".format(subject))
        hash_obj = md5()

        with open(subject, 'rb') as source_file:
            source_size = file_size(source_file)
            num_parts, chunk_size, tiny_size = calculate_chunks(source_size)
            key = Key()

            if num_parts == 1:
                source_file.seek(0)
                hash_obj.update(source_file.read(source_size))
            else:
                def gen_args(num):
                    for n in xrange(num):
                        yield (subject, n, chunk_size, key)

                pool = Pool(processes=self.thread_count)
                t1 = time.time()
                parts = pool.map_async(do_part_cksum, gen_args(num_parts)).get(9999999)
                # Print out some timings
                t2 = time.time() - t1

                s = source_size/1024./1024.
                logger.info("Finished hashing %0.2fM in %0.2fs (%0.2fMBps)" % (s, t2, s/t2))
                hash_obj.update(''.join(parts))

        hex_digest = hash_obj.hexdigest()
        if num_parts == 1:
            return hex_digest
        return '{0}-{1}'.format(hex_digest, num_parts)

    def verify(self, subject, target, copy=False):
        """compares hashes of a local subject and a remote target
        or those of two remote targets
        """
        bucket_name, key_name = Radula.split_bucket(target)
        if isinstance(target, (str, unicode)):
            target_key = guess_target_name(subject, key_name)
            target = "/".join([bucket_name, target_key])

        if copy:
            local_md5 = self.remote_md5(subject)
            remote_md5 = self.remote_md5(target)
        else:
            local_md5 = self.local_md5(subject)
            remote_md5 = self.remote_md5(target)

        if local_md5 == remote_md5:
            logging.info("Checksum Verified!")
            logging.info("Local and remote targets @ {0}".format(local_md5))
            return True
        else:
            logging.error("LocalMD5: {0} ; RemoteMD5: {1}".format(local_md5, remote_md5))
            print >> sys.stderr, "DIFFERENT CKSUMS!\nLocal {0}\nRemote {1}".format(local_md5, remote_md5)
            return False

    def multipart_list(self, subject, return_list=False):
        """lists lingering multipart upload parts """
        bucket_name, key_name = Radula.split_bucket(subject)
        bucket = self.conn.get_bucket(bucket_name)

        lines = []
        uploads = bucket.list_multipart_uploads()
        if key_name:
            uploads = [up for up in uploads if up.key_name == key_name]

        if return_list:
            return bucket, uploads

        for up in uploads:
            lines.append("\t".join((up.bucket.name, up.key_name, up.id, up.initiator.display_name, up.initiated)))

        return "\n".join(lines)

    def multipart_clean(self, subject):
        """alias of multipart_clean"""
        bucket, uploads = self.multipart_list(subject, return_list=True)
        for up in uploads:
            logging.info("Canceling {0} {1}".format(up.key_name, up.id))
            bucket.cancel_multipart_upload(up.key_name, up.id)
        return True

    def streaming_copy(self, source, destination, dest_profile=None, force=False, verify=False):
        """initiate streaming copy between two keys"""
        source_bucket_name, source_key_name = Radula.split_bucket(source)
        source_bucket = self.conn.get_bucket(source_bucket_name)
        source_key = source_bucket.get_key(source_key_name)
        if source_key is None:
            raise RadulaError("source key does not exist")

        if dest_profile is not None:
            if dest_profile == 'Default':
                dest_conn = self.new_connection()
            else:
                dest_conn = self.new_connection(dest_profile)
        else:
            dest_conn = self.conn

        dest_bucket_name, dest_key_name = Radula.split_bucket(destination)
        dest_key_name = guess_target_name(source, dest_key_name)

        dest_bucket = dest_conn.get_bucket(dest_bucket_name)
        dest_key = dest_bucket.get_key(dest_key_name)
        if dest_key is not None and not force:
            raise RadulaError("dest key exists (use -f to overwrite)")

        if not self._start_upload(source_key, dest_bucket, dest_key_name, verify, True, dest_conn):
            raise RadulaError("{0} did not correctly upload".format(source))

        return dest_key


def human_size(size, precision=2):
    """humanized units, ripped from the net"""
    suffixes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    suffix_index = 0
    while size >= 1024:
        suffix_index += 1

        size /= 1024.0
    return "%.*f %s" % (precision, size, suffixes[suffix_index])


def do_part_cksum(args):
    """hash one chunk of a local file.
    Several parts are run simultaneously."""
    subject, n, chunk_size, key = args
    with open(subject, 'rb') as source_file:
        source_file.seek(chunk_size * n)
        hex_digest, b64_digest = key.compute_md5(source_file, size=chunk_size)
    return binascii.unhexlify(str(hex_digest))


def do_part_upload(args):
    """
    Upload a part of a MultiPartUpload

    Open the target file and read in a chunk. Since we can't pickle
    S3Connection or MultiPartUpload objects, we have to reconnect and lookup
    the MPU object with each part upload.

    :type args: tuple of (string, string, string, int, int, int, boolean)
    :param args: The actual arguments of this method. Due to lameness of
                 multiprocessing, we have to extract these outside of the
                 function definition.

                 The arguments are: S3, Bucket name, MultiPartUpload id, file
                 name, the part number, part offset, part size, copy
    """
    s3, bucket_name, mpu_id, source_name, i, start, size, num_parts, copy, dest_conn = args
    logger.debug("do_part_upload got args: %s" % (args,))

    bucket = dest_conn.lookup(bucket_name)
    mpu = None
    for mp in bucket.list_multipart_uploads():
        if mp.id == mpu_id:
            mpu = mp
            break
    if mpu is None:
        raise Exception("Could not find MultiPartUpload %s" % mpu_id)

    # Read the chunk from the file
    if copy:
        range_query = "bytes=%d-%d" % (start, (start+size-1))
        resp = s3.make_request("GET", bucket=source_name[0], key=source_name[1], headers={'Range': range_query})
        data = resp.read()
    else:
        fp = open(source_name, 'rb')
        fp.seek(start)
        data = fp.read(size)
        fp.close()
    if not data:
        raise Exception("Unexpectedly tried to read an empty chunk")

    def progress(x, y):
        logger.debug("Part %d: %0.2f%%" % (i+1, 100.*x/y))

    try:
        # Do the upload
        t1 = time.time()
        mpu.upload_part_from_file(StringIO(data), i+1, cb=progress)

        # Print some timings
        t2 = time.time() - t1
    except Exception:
        logger.error("Error while uploading. ", exc_info=True)
        raise

    s = len(data)
    logger.info("Uploaded part %s of %s (%s) in %0.2fs at %sps" % (i+1, num_parts, human_size(s), t2, human_size(s/t2)))


def config_check():
    env_boto_config = os.environ.get("BOTO_CONFIG", None)
    boto_configs = [
        env_boto_config,
        os.path.expanduser("~/.boto"),
        os.path.expanduser("~/.aws/credentials")
    ]

    if env_boto_config and not os.path.exists(env_boto_config):
        message = "Environment variable BOTO_CONFIG is set to '{0}', " \
                  "but file not found"
        message = message.format(env_boto_config)
        print_warning(message)

    for config in boto_configs:
        if not config or not os.path.exists(config):
            continue
        mode = os.stat(config).st_mode
        if mode & 077:
            message = 'Boto config file "{0}" is mode {1}. Recommend ' \
                      'changing to 0600 to avoid exposing credentials'
            message = message.format(config, oct(mode & 0777))
            print_warning(message)

        if not os.access(config, os.R_OK):
            message = 'Config file {0} exists, but it is not readable.'
            message = message.format(config)
            print_warning(message)


def file_size(src):
    """quick file sizing"""
    src.seek(0, 2)
    return src.tell()


def print_timings(key, source_size, timing):
        timings = (human_size(source_size), timing, human_size(source_size / timing))
        logger.info("Finished uploading %s in %0.2fs (%sps)" % timings)
        logger.info("Download URL: {url}".format(url=url_for(key)))


def calculate_chunks(source_size):
    """generate a chunk count and size for a large file.
    tiny_size is also calculated for last-part folding"""
    _mb = 1024 * 1024
    _gb = _mb * 1024
    tiny_size = 25 * _mb
    default_chunk = 100 * _mb

    if source_size < tiny_size:
        return 1, source_size, tiny_size

    split_count = 50
    chunk_size = max(default_chunk, int(ceil(source_size / float(split_count))))
    chunk_size = min(_gb, chunk_size)
    chunk_count = int(ceil(source_size / float(chunk_size)))

    return chunk_count, chunk_size, tiny_size


def url_for(key):
    """proxy generate_url in boto.Key"""
    return re.sub("\?.+$", "", key.generate_url(expires_in=0, query_auth=False))


def guess_target_name(source_path, target_key):
    key_name = target_key
    basename = os.path.basename(source_path)
    if not key_name:
        key_name = basename
    elif key_name[-1] == "/":
        key_name = "".join([key_name, basename])
    return key_name


def print_warning(message):
    print '!' * 48
    print '! WARNING'
    print '! ' + message
    print '!' * 48
